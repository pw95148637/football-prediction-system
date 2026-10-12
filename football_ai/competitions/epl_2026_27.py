"""The first non-WC2026 CompetitionConfig instance: English Premier League,
2026-27 season (owner decision 2026-07-24 -- domestic round-robin league,
chosen from the near-term football calendar the owner supplied: EPL kicks
off 2026-08-22, the latest EPL season start on record, giving lead time to
build this before a ball is kicked; La Liga/Serie A/Bundesliga/Ligue 1 start
around the same window and would reuse the same
football_ai/data/football_data_co_uk.py adapter with a different division
code if a later window wants them -- not built here, out of scope for now).

Every declared number below is fit or grid-searched on REAL data (11 real
seasons, 2015-16..2025-26, 4180 matches, football-data.co.uk's E0.csv),
never copied from the WC2026 window -- see the "calibrations" store record
this era, appended by the calibration run this file's numbers came from
(`io.read("calibrations", era=ERA)`), for the full fit diagnostics
(OOT log-loss, gate decision, fit vs. full-window params).

GATE THRESHOLDS ARE BACKTESTED, WITH AN HONEST NEGATIVE RESULT (experiments
registry, exp_id="epl_gate_threshold_backtest_2026-07-24",
`football_ai.audit.experiments.find(...)`): staking flat 1u on the
model-favored side whenever it beat the Shin-devigged Pinnacle CLOSING
price by >= threshold, on 970 out-of-time EPL matches (2023-08 onward, held
out from the fit), NO threshold from 0-15pp produced a bootstrap 95% CI
excluding zero ROI. Naive gap-only disagreement with the closing line is
NOT a demonstrated edge for EPL -- which is exactly why the two-question
mechanism gate exists downstream of the pp threshold (DECISION_PROTOCOL.md):
gap was never supposed to be sufficient on its own. WATCH_PP/BET_PP below
are therefore set as investigation-worthiness triggers (the ~70th/~88th
percentile of the observed gap distribution -- "rare enough a human/LLM
should look for a mechanism"), explicitly NOT as EV-validated auto-bet
cutoffs. This is a materially different (much stricter) role than WC2026's
3pp/5pp played for a hand-curated 16-match sample; do not read them as
"the EPL version of the same claim."

OPERATIONAL FUNCTIONS below (current_ratings/lock_match/report/settle_match,
added 2026-07-24 so a session can drive this competition by name -- "鎖
Arsenal 對 Coventry", not by writing a fresh Python snippet each time; see
.claude/skills/epl-2026-27/SKILL.md for the natural-language runbook) --
registry.py/agenda.py/decision/report.py/evidence/schema.py/market/clv.py
are all migrated and config-aware as of 2026-07-24 (generalization prompt
task 5 substantially done for this competition's needs).

FIXED 2026-08-26 (the season's first live session; all three were real
defects, not cosmetics -- see each function's own docstring):
  - `current_ratings()` defaulted to a hardcoded `range(2015, 2026)`, which
    EXCLUDED the season in progress. On 2026-08-26, with matchweek 1
    played, that left every rating a full matchweek stale (Man United +31
    Elo, Hull -31, because Hull 2-0 Man United was not in the replay).
    Now defaults through `current_season_start_year()`.
  - `lock_match()` wrote the literal string
    "not-a-cryptographic-lock-hash-yet" as its sha256: locks had a
    timestamp but zero tamper evidence. Now hashes the payload with the
    archived lock.py's exact canonical-JSON convention.
  - there was no amend path at all, so instrument J had nowhere to land.
    `amend_lock()` now exists, and mechanically refuses an amend written
    after an odds snapshot for that match is in the store (the m97
    contamination lesson) unless the caller explicitly accepts a permanent
    `contaminated_post_odds` flag on the record.

LOCK PROVENANCE, matchweek 1 (settled 2026-08-26): the two MW1 locks
predate the sha256 fix, and one of them (`epl2627_gw01_ars_cov`) carries a
literal "demo-not-a-real-lock-hash" and no `inputs` block, so its status as
a real lock was genuinely in doubt. Resolved by recomputation rather than
by judgment: replaying the Elo through completed seasons only (the ratings
as they stood at the 2026-07-24 lock time) and re-deriving lambdas/probs
reproduces BOTH stored payloads exactly (<1e-9 on every field). The M
estimates are authentic model output; only the metadata was placeholder.
Both are settled (Arsenal 3-0 Coventry, Man City 2-1 Bournemouth).

STILL OPEN (as of 2026-10-06; the three items that used to be listed here --
schedule beyond MW3, odds ingestion, J wiring -- are all done: the schedule
is in epl_2026_27_fixtures.py, `ops.snapshot` ingests odds, `ops.run_j` runs
instrument J):
  - kickoff times from matchweek 10 onward are provisional, so those
    fixtures cannot be locked until the time is confirmed (lock_match
    refuses a fixture with none). How to refresh them is in
    epl_2026_27_fixtures.py ("HOW TO REFRESH").
  - football-data.co.uk's fixtures.csv is NOT a schedule source: it is a
    next-day-or-two matchday feed, and every row carries pre-match
    bookmaker odds columns, so it contaminates whoever reads it raw.
  - a competition can be paused by the owner (a `paused` entry in
    competitions/__init__.py); every fixture that kicks off unlocked is
    gone for good. `sample.mode="dynamic"` means nothing breaks meanwhile.
"""
import hashlib
import json
import math
from datetime import datetime, timezone

from football_ai.core.competition import (
    CompetitionConfig, DataSources, EloTierTable, GateThresholds,
    HomeAdvantageModel, SampleDefinition,
)

ERA = "v1-epl-2026-27"

# Evidence categories legal for this window. `CompetitionConfig` already
# carries the club-competition list as a field default; this name exists so
# callers can write `EVIDENCE_CATEGORIES` without reaching into CONFIG, and
# it DELEGATES rather than restating -- a second hand-typed copy of the same
# list is how the two silently diverge. The WC2026 default in
# evidence/schema.py includes "illness_camp", a national-team concept (a
# squad living together in a tournament camp); a club side has no camp, so
# offering the category invites a scout to file an ordinary injury under it.
EVIDENCE_CATEGORIES = None  # bound below, once CONFIG exists

# Real football-data.co.uk EPL fixture-id shape has no natural sequence
# number (dated home/away pairs, not "matchN"); this convention gives one
# for basic gameweek ordering. NOT yet populated against a real fixture
# list -- see module docstring.
_MATCH_ID_PATTERN = r"^epl\d{4}_gw(\d+)_"

CONFIG = CompetitionConfig(
    competition_id="epl-2026-27",
    era=ERA,
    format="round_robin",
    sample=SampleDefinition(mode="dynamic", match_id_pattern=_MATCH_ID_PATTERN),
    # home_adv_elo=60: grid-searched over {30,45,...,120} for best OOT score
    # log-loss (calibrations record, era=ERA). Notably lower than WC2026's
    # HOME_ADV=100 -- consistent with the well-documented shrinkage of
    # domestic-league home advantage through the 2020s; NOT a copy of the
    # WC2026 constant.
    home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
    # default_k=25: grid-searched jointly with home_adv_elo; EPL is a
    # single competition (no friendly/qualifier/final-tournament mix), so
    # an empty `tiers` dict is correct -- everything uses default_k.
    elo_tiers=EloTierTable(default_k=25),
    # Judgment call, not a fit or a copy of WC2026's MIN_N=5: a full EPL
    # season is ~380 matches (vs. WC2026's 16), so a materially larger
    # sample is available before claiming a mechanism type has an edge.
    # 12 is a starting point, not a derived-optimal value -- revisit once
    # real mechanism-taxonomy claims start accumulating for this era.
    mechanism_min_n=12,
    # Backtested (module docstring + experiments registry
    # exp_id="epl_gate_threshold_backtest_2026-07-24"): no gap threshold
    # showed a statistically significant ROI edge against Pinnacle closing
    # lines, so these are investigation-worthiness triggers (~70th/~88th
    # percentile of the real gap distribution), not EV-validated cutoffs --
    # the two-question mechanism gate remains the actual bar for betting.
    # stake_u=1.0 and max_bets_per_match=2 are unit-convention /
    # correlation-control choices, not backtested quantities -- same
    # universal role they play for WC2026, kept as sensible defaults.
    gate=GateThresholds(watch_pp=6.5, bet_pp=10.0, stake_u=1.0,
                         max_bets_per_match=2),
    # Measured on THIS window's own data, 2026-09-02. Replicated twice with
    # independently written bucketing before being written down here.
    j_spec_version="rr1",
    known_biases=(
        "M's probabilities are BEATEN BY THE CLOSING PRICE out of sample: "
        "mean RPS gap +0.0075 [+0.0039, +0.0110] over 970 EPL matches from "
        "2023-08 with a Pinnacle close, and +0.0225 [+0.0016, +0.0435] on "
        "the subset where M disagrees with the close by >=10pp. The bigger "
        "the disagreement, the more likely it is M that is wrong -- a warning "
        "about M's number, not an opportunity. Measured on M ALONE: it says "
        "nothing about a judgment that departs from M on its own evidence. "
        "J has not been scored against the close; the judgment layer is "
        "scored by its own record (decision.track_record, in the summary's "
        "fine print), not presumed from M's",
        "the ROI consequence of that gap is NOT established: two independent "
        "replications produced opposite signs (-10.3% vs +13.0%) on "
        "differently-bucketed subsets and the n=120 interval spans -18% to "
        "+49%. The probability-quality finding is solid; any profit claim "
        "derived from it is unmeasured and must not be cited as one",
        "the goal curve maps effective Elo difference to lambdas and is "
        "blind to team-specific attack/defence identity, so totals and "
        "handicap EVs inherit a known blind spot rather than free money "
        "(diagnosed on the WC2026 window over 6+ matches; not yet "
        "re-measured on this league's own sample)",
        "Elo is built purely from RESULTS and cannot see squad composition. "
        "Where a club's personnel changed materially since the ratings were "
        "earned, M is stale in a direction the market can see and M cannot. "
        "J (j_spec rr1, mechanism_type squad_composition_change) and the "
        "judgment layer may correct it, citing squad facts and never the "
        "market; M itself stays blind",
    ),
    data_sources=DataSources(
        rating_source="self_computed_elo_replay",  # models/m_dc.py replay_df()
        results_source="football_data_co_uk:E0",
        odds_book_default="pinnacle",
        odds_region_default="eu",
    ),
)


# Full-window fit params from the calibrations record this era's
# _selfcheck/_backtest run persisted (io.read("calibrations", era=ERA)) --
# read once at import time since they don't change between calls; a
# fresh calibration run (re-fitting on more seasons) replaces the store
# record and this module picks it up on next import, not silently at
# runtime (avoids a report changing mid-session for no visible reason).
EVIDENCE_CATEGORIES = list(CONFIG.evidence_categories)


def _fit_params():
    from football_ai.store import io as store_io
    recs = [r["data"] for r in store_io.read("calibrations", era=ERA)
            if str(r["data"].get("model", "")).startswith("m_dc")
            and r["data"].get("gate", {}).get("decision") == "adopted"]
    if not recs:
        raise RuntimeError(
            f"no adopted m_dc calibration for era={ERA!r} in the store's "
            f"`calibrations` ledger. That ledger is append-only, so first "
            f"recover it from git (`git log -- "
            f"football_ai/store/data/calibrations.jsonl`); refitting changes "
            f"the instrument and needs the evaluator (DECISION_PROTOCOL "
            f"section 9) before any match is locked")
    p = recs[-1]["full_window_params"]
    return p["g_a"], p["g_b"], p["rho"]


def goal_curve():
    """(g_a, g_b, rho) — this competition's fitted goal curve, as a public
    entry point. `competitions/__init__.ACTIVE` registers it so
    `ops.run_j()` can fill `inputs.goal_curve` on a lock written before
    locks carried one, without reaching into a private name or, worse,
    a caller retyping the three numbers next to the wrong key."""
    return _fit_params()


FIRST_RATING_SEASON = 2015  # 2015-16, the earliest season the Elo replay reads


def current_season_start_year(today=None):
    """Start year of the season in progress -- delegated to the data source
    that owns the fact. It lived here first, then the same August->May
    boundary was needed in `football_data_co_uk` to decide which season's
    cached file may go stale; two copies of a date rule is exactly the kind
    of thing that drifts silently, so there is one."""
    from football_ai.data.football_data_co_uk import (
        current_season_start_year as _impl)
    return _impl(today)


def current_ratings(start_years=None):
    """Real Elo ratings for every team that has played in the top flight in
    `start_years`, replayed chronologically through the most recent result
    on file. A team absent from top-flight history (newly promoted) is
    simply absent from the returned dict -- callers should treat a missing
    team as rating 1500.0 (the same default the replay itself uses for a
    team's first-ever fixture), not as an error.

    `start_years=None` (default) means FIRST_RATING_SEASON through the
    season CURRENTLY IN PROGRESS, inclusive. That last part is the whole
    point and was a real bug when this defaulted to a hardcoded
    `range(2015, 2026)`: on 2026-08-26, with matchweek 1 already played,
    every rating was stale by a full matchweek -- Man United 31 Elo too
    high and Hull 31 too low, because Hull 2-0 Man United was simply not in
    the replay. Locking a matchweek-2 fixture off those numbers would have
    put several percentage points of error into M before anything else
    happened. The in-progress season is loaded with `missing_ok=True` so
    this still works in the pre-season weeks before its file exists."""
    import pandas as pd
    from football_ai.data.football_data_co_uk import load_seasons
    from football_ai.models.m_dc import gd_mult

    if start_years is None:
        start_years = range(FIRST_RATING_SEASON,
                            current_season_start_year() + 1)
    rows = load_seasons("E0", start_years, missing_ok=True)
    df = pd.DataFrame(rows)
    rating = {}
    for r in df.sort_values("date").itertuples(index=False):
        eh = rating.get(r.home_team, 1500.0)
        ea = rating.get(r.away_team, 1500.0)
        d_eff = eh - ea + CONFIG.home_advantage.home_adv_elo
        hs, as_ = r.home_score, r.away_score
        w = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        we = 1.0 / (1.0 + 10.0 ** (-d_eff / 400.0))
        delta = CONFIG.elo_tiers.default_k * gd_mult(abs(hs - as_)) * (w - we)
        rating[r.home_team] = eh + delta
        rating[r.away_team] = ea - delta
    return rating


def _canonical_sha(payload):
    """sha256 over the canonical JSON of the estimate payload -- byte-for-
    byte the archived lock.py convention (`sort_keys=True,
    ensure_ascii=False`), so a hash computed here and one computed there
    over the same payload agree. Until 2026-08-26 this competition wrote
    the literal string "not-a-cryptographic-lock-hash-yet" instead, i.e.
    the locks had a timestamp but no tamper evidence at all."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_payload(payload, match_id):
    """Minimal shape check shared by lock_match/amend_lock."""
    if payload.get("match_id") != match_id:
        raise ValueError(
            f"REFUSED: payload match_id {payload.get('match_id')!r} does not "
            f"match {match_id!r}")
    for key in ("teams", "kickoff_utc", "probs_90"):
        if not payload.get(key):
            raise ValueError(f"REFUSED: payload is missing {key!r}")
    probs = payload["probs_90"]
    missing = [k for k in ("home", "draw", "away") if k not in probs]
    if missing:
        raise ValueError(f"REFUSED: probs_90 is missing {missing}")
    total = sum(probs[k] for k in ("home", "draw", "away"))
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"REFUSED: probs_90 sums to {total!r}, not 1.0")


def lock_match(match_id, home_team, away_team, kickoff_utc, ratings=None):
    """Compute the M instrument (Elo + Dixon-Coles) for one fixture and
    write it to the store as a version=1 lock under this competition's era.

    Mirrors the non-negotiable "lock before market" discipline (the
    generalization prompt's principle 1, and the archived lock.py's own
    `_check_pre_kickoff`): refuses to run once `kickoff_utc` has passed,
    and refuses to silently overwrite an existing lock for this match_id
    (amending a lock is a separate, deliberate action this function does
    not perform: see `amend_lock()`, which demands a real "what new
    information justifies this" reason, not a shortcut).

    `ratings`: an optional pre-computed current_ratings() dict, so a
    caller locking several matches in one session computes Elo once, not
    once per match. Defaults to computing it fresh.
    """
    from football_ai.models.m_dc import score_matrix, outcome_probs
    from football_ai.store import io as store_io

    if not kickoff_utc:
        raise ValueError(
            f"REFUSED: {match_id!r} has no confirmed kickoff. The kickoff is "
            f"written into the lock permanently and the closing-price window "
            f"is measured from it, so a guessed time must not be recorded. "
            f"Confirm it against the Premier League's fixture pages (see the "
            f"docstring of epl_2026_27_fixtures.py), fill kickoff_utc there, "
            f"then lock.")
    ko = datetime.fromisoformat(kickoff_utc)
    now = datetime.now(timezone.utc)
    if ko < now:
        raise ValueError(
            f"REFUSED: kickoff {kickoff_utc} has passed (now "
            f"{now.isoformat()}) -- a lock after kickoff is never honest "
            f"(the same discipline the archived lock.py enforces)")
    # a lock another machine already wrote is invisible to a stale checkout
    from football_ai.store import sync as store_sync
    store_sync.assert_current()
    existing = [r for r in store_io.read("locks", era=ERA)
                if r.get("match_id") == match_id]
    if existing:
        raise ValueError(
            f"REFUSED: {match_id!r} already has a lock in the store "
            f"(era={ERA!r}) -- amend with amend_lock(), don't silently "
            f"overwrite")

    # A misspelled club silently gets the 1500 default Elo below, and the
    # lock is permanent: refuse names the competition does not know.
    from football_ai.competitions.epl_2026_27_fixtures import ROSTER
    unknown = [t for t in (home_team, away_team) if t not in ROSTER]
    if unknown:
        raise ValueError(
            f"REFUSED: {unknown} not in ROSTER. Use football-data.co.uk's "
            f"spelling (e.g. 'Man City', \"Nott'm Forest\"); a name that "
            f"misses would silently be rated 1500.0 and locked that way.")

    ratings = ratings if ratings is not None else current_ratings()
    eh = ratings.get(home_team, 1500.0)
    ea = ratings.get(away_team, 1500.0)
    d_eff = eh - ea + CONFIG.home_advantage.home_adv_elo
    g_a, g_b, rho = _fit_params()
    lam_h = math.exp(g_a + g_b * d_eff / 100.0)
    lam_a = math.exp(g_a - g_b * d_eff / 100.0)
    w, d, a = outcome_probs(lam_h, lam_a, rho)

    payload = {
        "match_id": match_id, "pilot": False,
        "teams": {"home": home_team, "away": away_team},
        "kickoff_utc": kickoff_utc,
        "probs_90": {"home": w, "draw": d, "away": a},
        "lambdas": {"home": lam_h, "away": lam_a},
        "inputs": {"elo_home": eh, "elo_away": ea, "elo_diff_effective": d_eff,
                   # the curve J's arithmetic gate recomputes from
                   # (j_runner.implied_probs_from_factors). Key names are
                   # that function's; before 2026-09-03 the lock omitted
                   # this and the skill told the caller to add it under
                   # the wrong key ("rho"), so the one live J was never
                   # arithmetic-checked.
                   "goal_curve": {"g_a": g_a, "g_b": g_b,
                                  "dixon_coles_rho": rho}},
    }
    _validate_payload(payload, match_id)
    return store_io.append(
        "locks",
        {"locked_at": now.isoformat(), "version": 1, "payload": payload,
         "sha256": _canonical_sha(payload)},
        era=ERA, match_id=match_id, version=1)


def amend_lock(match_id, payload, reason, acknowledged_post_odds=False):
    """Write a version=N+1 lock for `match_id`, citing real new information.

    This is how instrument J reaches a lock: run `research/j_runner.py`
    against the version-1 M payload plus a firewall-validated evidence
    bundle, then amend the result in with `payload["j"] = <j block>`. It is
    also how a genuine pre-kickoff M revision is recorded. The original
    version is never touched -- amending appends, exactly like the archived
    `lock.py` (locks are immutable; the amend carries the original's hash).

    Guards, in the order they fire:
      - a concrete `reason` is mandatory (empty/whitespace is refused);
      - kickoff must not have passed, and `payload["kickoff_utc"]` must
        match the original's -- an amend may revise the estimate, never
        quietly restate when the match starts;
      - **an existing odds snapshot for this match refuses the amend.**
        That is the mechanical form of the contamination rule: once market
        prices for this fixture are in the store, whoever writes a new
        estimate may have seen them, and "J locked after first market
        contact" is precisely what the archived score.py audits for and
        retires an arm over (m97). Passing
        `acknowledged_post_odds=True` proceeds anyway BUT stamps
        `contaminated_post_odds: true` into the record -- declared, never
        hidden, so any later scoring of this era can exclude it
        mechanically instead of relying on someone remembering.
    """
    from football_ai.store import io as store_io
    from football_ai.store import sync as store_sync

    if not reason or not str(reason).strip():
        raise ValueError(
            "REFUSED: an amend requires a concrete new-information reason "
            "(a market price is never new information)")

    # The odds guard below reads the LOCAL odds file. A checkout that is
    # behind origin (the hourly job stores prices there) cannot see a price
    # that exists, so this guard would fail open.
    store_sync.assert_current()

    existing = [r for r in store_io.read("locks", era=ERA)
                if r.get("match_id") == match_id]
    if not existing:
        raise ValueError(
            f"REFUSED: no lock exists for {match_id!r} (era={ERA!r}) -- "
            f"create one with lock_match() first; an amend has nothing to "
            f"amend")
    original = min(existing, key=lambda r: r.get("version", 1))

    _validate_payload(payload, match_id)
    base_ko = original["data"]["payload"].get("kickoff_utc")
    if payload.get("kickoff_utc") != base_ko:
        raise ValueError(
            f"REFUSED: amend changes kickoff_utc "
            f"({base_ko!r} -> {payload.get('kickoff_utc')!r}) -- an amend "
            f"revises the estimate, not the fixture")
    ko = datetime.fromisoformat(payload["kickoff_utc"])
    now = datetime.now(timezone.utc)
    if ko < now:
        raise ValueError(
            f"REFUSED: kickoff {payload['kickoff_utc']} has passed (now "
            f"{now.isoformat()}) -- a post-kickoff amend is post-hoc "
            f"adjustment, never honest")

    has_odds = any(r.get("match_id") == match_id
                   for r in store_io.read("odds", era=ERA))
    if has_odds and not acknowledged_post_odds:
        raise ValueError(
            f"REFUSED: {match_id!r} already has an odds snapshot in the "
            f"store -- an estimate written after market contact is "
            f"contaminated (the m97 lesson), and a clean J is foreclosed for "
            f"this fixture. The correct outcome is to record the loss as a "
            f"non-random gap (M-only). Re-call with acknowledged_post_odds="
            f"True only if the store's own timestamps prove this estimate was "
            f"formed before the snapshot; the record then carries "
            f"contaminated_post_odds=true permanently")

    version = max(r.get("version", 1) for r in existing) + 1
    data = {"locked_at": now.isoformat(), "version": version,
            "amend_reason": str(reason).strip(),
            "original_sha256": original["data"].get("sha256"),
            "payload": payload, "sha256": _canonical_sha(payload)}
    if has_odds:
        data["contaminated_post_odds"] = True
    return store_io.append("locks", data, era=ERA, match_id=match_id,
                           version=version)


def report(match_id, mechanism=None, abstention=None):
    """Convenience wrapper: decision.report.build()+render() bound to this
    competition's config. Returns the rendered text.

    `abstention` = {"missing", "flips_if", "known_by"} (DECISION_PROTOCOL
    section 4). Any PASS/WATCH rendered without it carries UNFINISHED."""
    from football_ai.decision.report import build, render
    return render(build(match_id, mechanism=mechanism, config=CONFIG,
                        abstention=abstention))


# |gap| at or above which a fixture sits in the band where this window's own
# measurement says M, not the market, is the likelier error (see
# CONFIG.known_biases; STAKING.md section 2.2). Passed to
# sizing.gap_bias_pattern as the threshold. It equals CONFIG.gate.bet_pp today
# by coincidence of two separate measurements, not by definition: one is the
# gate's "worth examining" line, the other where M loses to the closing price.
BIAS_PATTERN_PP = 10.0


def evaluate_gate(gap_pp, mechanism=None, checklist=None, abstention=None):
    """The two-question gate with EPL's thresholds (`CONFIG.gate`). Use this,
    never a bare `sop.evaluate`, for a hand-made BET_CANDIDATE review."""
    from football_ai.decision import sop
    return sop.evaluate_for(CONFIG, gap_pp, mechanism, checklist, abstention)


def settle_match(match_id, home_score, away_score):
    """Write the final score to the store's "results" kind under this
    competition's era, in the same {"match_id", "score90"} shape the
    archived pipeline and registry/agenda's settlement checks expect.
    No "advanced" field -- that's a knockout-qualify concept with nothing
    to record for a round-robin league."""
    from football_ai.store import io as store_io
    return store_io.append(
        "results",
        {"match_id": match_id, "score90": [home_score, away_score]},
        era=ERA, match_id=match_id)


def _selfcheck_body():
    fails = []
    if CONFIG.knockout is not None:
        fails.append("EPL is round_robin, knockout must be None")
    if CONFIG.sample.mode != "dynamic":
        fails.append("EPL has no pre-registered fixed sample")
    if CONFIG.home_advantage.mode != "standard":
        fails.append("EPL fixtures have real home venues, not neutral_default")
    if CONFIG.era == "v2-wc2026" or CONFIG.era == "v3":
        fails.append("EPL era must not collide with the WC2026 eras")

    # Season boundary: July onward belongs to the season starting that year,
    # so the in-progress season is always included in the rating replay.
    from datetime import date as _date
    for day, expected in ((_date(2026, 8, 26), 2026), (_date(2027, 3, 1), 2026),
                          (_date(2026, 6, 30), 2025), (_date(2026, 7, 1), 2026)):
        got = current_season_start_year(day)
        if got != expected:
            fails.append(f"current_season_start_year({day}) should be "
                         f"{expected}, got {got}")

    # --- operational functions, against a temp store (never the real one;
    # ratings passed in directly so this stays offline/fast, no HTTP) ---
    import tempfile
    from datetime import timedelta
    from pathlib import Path
    from football_ai.store import io as store_io

    old_store = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            store_io.append("calibrations", {
                "model": "m_dc (generalized replay_df)",
                "gate": {"decision": "adopted"},
                "full_window_params": {"g_a": 0.2954, "g_b": 0.1644, "rho": -0.0372},
            }, era=ERA)

            future_ko = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            fake_ratings = {"Arsenal": 1837.5}  # Coventry absent -> 1500.0 default
            rec = lock_match("t_ars_cov", "Arsenal", "Coventry", future_ko,
                             ratings=fake_ratings)
            if rec["data"]["payload"]["probs_90"]["home"] < 0.5:
                fails.append(
                    f"lock_match: Arsenal (1837.5) at home vs a 1500-default "
                    f"Coventry should be clearly favored: {rec['data']['payload']['probs_90']}")

            try:
                lock_match("t_ars_cov", "Arsenal", "Coventry", future_ko,
                          ratings=fake_ratings)
                fails.append("lock_match must refuse to silently re-lock an "
                             "already-locked match_id")
            except ValueError:
                pass

            past_ko = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            try:
                lock_match("t_past", "A", "B", past_ko, ratings={})
                fails.append("lock_match must refuse a kickoff that has already passed")
            except ValueError:
                pass

            try:
                lock_match("t_typo", "Arsnal", "Coventry", future_ko,
                           ratings=fake_ratings)
                fails.append("lock_match must refuse a club name not in ROSTER "
                             "(a typo would silently be rated 1500)")
            except ValueError:
                pass

            rendered = report("t_ars_cov")
            if "Arsenal" not in rendered or "Coventry" not in rendered:
                fails.append(f"report() did not render the locked fixture: {rendered[:200]}")

            settle_match("t_ars_cov", 3, 0)
            results = store_io.read("results", era=ERA)
            settled = next((r["data"] for r in results
                           if r["data"].get("match_id") == "t_ars_cov"), None)
            if settled is None or settled.get("score90") != [3, 0]:
                fails.append(f"settle_match did not persist the expected score90: {settled}")
            if "advanced" in (settled or {}):
                fails.append("settle_match must not write a knockout-only 'advanced' field")

            # --- tamper evidence: the hash must be real and reproducible ---
            import re as _re
            base_payload = rec["data"]["payload"]
            from football_ai.research.j_runner import implied_probs_from_factors
            if implied_probs_from_factors(rec["data"]["payload"], []) is None:
                fails.append("a fresh lock must carry inputs.goal_curve in "
                             "j_runner's key names, or J's arithmetic gate "
                             "silently never runs")
            digest = rec["data"].get("sha256")
            if not _re.fullmatch(r"[0-9a-f]{64}", digest or ""):
                fails.append(f"lock_match must write a real sha256, got {digest!r}")
            if digest != _canonical_sha(base_payload):
                fails.append("lock sha256 does not reproduce from its own payload")

            # --- amend_lock guards ---
            amended = dict(base_payload)
            amended["j"] = {"probs_90": {"home": 0.70, "draw": 0.20, "away": 0.10}}

            try:
                amend_lock("t_ars_cov", amended, "   ")
                fails.append("amend_lock must refuse a blank new-information reason")
            except ValueError:
                pass
            try:
                amend_lock("t_no_such_match", amended, "J from evidence bundle")
                fails.append("amend_lock must refuse a match with no base lock")
            except ValueError:
                pass
            moved = dict(amended)
            moved["kickoff_utc"] = (datetime.now(timezone.utc)
                                    + timedelta(days=8)).isoformat()
            try:
                amend_lock("t_ars_cov", moved, "J from evidence bundle")
                fails.append("amend_lock must refuse an amend that moves kickoff_utc")
            except ValueError:
                pass

            a2 = amend_lock("t_ars_cov", amended, "J from evidence bundle")
            if a2.get("version") != 2:
                fails.append(f"first amend should be version 2, got {a2.get('version')}")
            if a2["data"].get("original_sha256") != digest:
                fails.append("amend must carry the original lock's sha256")
            if a2["data"].get("sha256") == digest:
                fails.append("an amend with a changed payload must hash differently")
            if a2["data"].get("contaminated_post_odds"):
                fails.append("a pre-odds amend must not be flagged contaminated")

            # once market prices exist for this match, an amend is refused
            # unless the caller explicitly accepts the permanent flag
            store_io.append("odds", {"match_id": "t_ars_cov", "book": "stub",
                                     "market": "1x2", "line": None,
                                     "outcomes": ["home", "draw", "away"],
                                     "odds": [1.5, 4.2, 6.5],
                                     "fetched_at": "2026-01-01T00:00:00+00:00"},
                            era=ERA, match_id="t_ars_cov")
            try:
                amend_lock("t_ars_cov", amended, "second thoughts")
                fails.append("amend_lock must refuse once an odds snapshot exists "
                             "(post-market estimate is contaminated)")
            except ValueError:
                pass
            a3 = amend_lock("t_ars_cov", amended, "declared post-odds amend",
                            acknowledged_post_odds=True)
            if not a3["data"].get("contaminated_post_odds"):
                fails.append("an acknowledged post-odds amend must be stamped "
                             "contaminated_post_odds=true, not written clean")
            if a3.get("version") != 3:
                fails.append(f"second amend should be version 3, got {a3.get('version')}")
            # the flag must be READ BACK: a contaminated J has to say so in
            # the report, not render like a clean one
            try:
                if "contaminated_post_odds" not in report("t_ars_cov"):
                    fails.append("report() must disclose a contaminated_post_odds "
                                 "J in its honesty block")
            except Exception as e:  # noqa: BLE001
                fails.append(f"report() on a contaminated lock raised {e!r}")
        finally:
            store_io.STORE_DIR = old_store

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")


def _selfcheck():
    """Run the module's checks offline (the sync check would otherwise fetch
    from the real origin), then prove the guards really call it."""
    import os
    from football_ai.store import sync as store_sync

    saved = os.environ.get(store_sync.SKIP_ENV)
    os.environ[store_sync.SKIP_ENV] = "1"
    try:
        fails = _selfcheck_body()
    finally:
        if saved is None:
            os.environ.pop(store_sync.SKIP_ENV, None)
        else:
            os.environ[store_sync.SKIP_ENV] = saved

    # a checkout behind origin must stop lock_match and amend_lock cold
    real = store_sync.assert_current

    def _behind(*a, **k):
        raise store_sync.SyncError("behind origin (test)")

    store_sync.assert_current = _behind
    try:
        from datetime import timedelta
        ko = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        for name, call in (
                ("lock_match", lambda: lock_match(
                    "t_sync", "Arsenal", "Coventry", ko, ratings={})),
                ("amend_lock", lambda: amend_lock("t_sync", {}, "real reason"))):
            try:
                call()
                fails.append(f"{name} must refuse while the checkout is "
                             f"behind origin")
            except store_sync.SyncError:
                pass
    finally:
        store_sync.assert_current = real
    return fails
