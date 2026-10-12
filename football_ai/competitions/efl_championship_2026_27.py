"""EFL Championship 2026-27: trial window, registered PAUSED.

Owner authorization 2026-10-10 (AGENT_DISPATCH section 7, "英冠試行"): add a
Championship window that runs M and closing-price scoring first, with the
judgment layer sampled later. The charter names a softer market as the right
target (DECISION_PROTOCOL section 10); this window is where that is tested.
It is registered paused: the owner decides when it starts.

M is the EPL's instrument family -- an Elo replay of results plus the
Dixon-Coles goal curve of models/m_dc.py, results only -- with one addition
the division needs. Six of 24 clubs are new every summer, three relegated
from the Premier League and three promoted from League One, and results
inside one division say nothing about how strong an arriving club is. The
EPL replay would rate a never-seen club 1500 and a returning one at a
possibly decade-old rating. Here an arriving club enters at the mean of the
continuing clubs, +100 if it came down from the Premier League and -75 if
it came up from League One (models/division_elo.py, rule "offset").

Every number in M was chosen on 2015-08..2023-08 Championship results only
and then scored out of time against the Pinnacle closing price on
2023-08..2026-07 -- research/championship_backtest/RESULTS.md, reproducible
with research/championship_backtest/backtest.py. No price is read to build M.

NOT WIRED, ON PURPOSE:
  - J and the judgment layer. `amend_lock` refuses; registering it as a
    callable is what keeps `ops.run_j` from reaching this window by accident.
  - The fixture list. `FIXTURES` is empty, so `ops lock` refuses every
    Championship fixture ("not in this competition's fixture list") and the
    agenda has nothing to remind about. football-data.co.uk's fixtures.csv
    is not a schedule source (it carries bookmaker odds; see epl_2026_27.py).
  - The gate thresholds below follow the EPL's own rule (about the 70th and
    88th percentile of M's positive gap to the close) and have NOT
    been reviewed by the evaluator (DECISION_PROTOCOL section 9 reserves
    gate constants to the evaluator). Until reviewed they are provisional.

ACTIVATION CHECKLIST -- each item is a prerequisite, in this order:
  0. EVALUATOR DECISION (DECISION_PROTOCOL section 9): the EPL's Dixon-Coles
     admission gate REJECTS the curve on this division. Out of time
     (2023-08..2026-07, n=1656) DC's exact-score log-loss is 2.839262
     against independent Poisson's 2.839208 -- worse by 0.00005, a tie in
     substance but a fail by the rule (`calibration_record()["gate"]`).
     `calibration --append` therefore refuses, and so `lock_match` cannot
     run. Options: (a) adopt the same curve with rho = 0 (Elo + independent
     Poisson; the 1X2 consequence is in RESULTS.md's rho = 0 row); (b)
     adopt DC anyway as a recorded override of the gate; (c) keep it
     closed. Not decided here.
  1. Main session: append the goal-curve calibration the evaluator chose
     (`python -m football_ai.competitions.efl_championship_2026_27
     calibration --append`; without `--append` it only prints). Until then
     `lock_match` refuses: there is no adopted curve for this era.
  2. A fixture module with confirmed kickoff times (shape of
     epl_2026_27_fixtures.FIXTURES; ids match `_MATCH_ID_PATTERN`), wired
     into this competition's ACTIVE entry as `upcoming_fixtures`.
  3. `ops` must take the odds sport key from the competition, not from its
     CLI default: `ops --sport-key` defaults to "soccer_epl" whatever
     `--competition` says, so a Championship snapshot run without the flag
     would spend quota on the Premier League board. Read `ODDS_SPORT_KEY`
     below (or an ACTIVE-entry key) in `ops.main` and `ops._matchday`.
  4. The unattended job (`epl-matchday`; another session is editing it, so
     it is described here, not changed): `ops matchday` runs one competition
     (`_matchday("epl-2026-27")` via ops.main's default). It needs to loop
     over every ACTIVE, non-paused competition, each step with that
     competition's `--competition` and sport key, keeping "one failing step
     must not stop the next" per competition; the API-call gate
     (`--require-fixture-within-min`) is already per competition, and the
     cron hours (07-22 UTC) already cover Championship kickoffs. The
     workflow's results-snapshot step calls `refresh-snapshot` with its
     defaults (division E0, from 2015); this replay reads E1 and E0 from
     2005-06, so the price-free fallback needs `--division E1 --from-year
     2005` and E0 `--from-year 2005`, or a session that cannot reach
     football-data.co.uk cannot rate this division. `concurrency:
     ledger-writer` and the publish step need no change.
  5. The owner lifts the pause in competitions/__init__.py.
"""
import math
from datetime import datetime, timezone

from football_ai.core.competition import (
    CompetitionConfig, DataSources, EloTierTable, GateThresholds,
    HomeAdvantageModel, SampleDefinition,
)

ERA = "v1-efl-championship-2026-27"

# football-data.org's code for the Championship is "ELC"; ids read
# elc2627_gw07_wat_bur (gameweek = Championship matchday, 1..46).
_MATCH_ID_PATTERN = r"^elc\d{4}_gw(\d+)_"

# the-odds-api's sport key for this division (owner brief, 2026-10-10).
ODDS_SPORT_KEY = "soccer_efl_champ"

DIVISION = "E1"            # football-data.co.uk division code
DIVISION_ABOVE = "E0"      # where a club "from above" was last season
REPLAY_FROM = 2005         # 2005-06: first season of the replay (burn-in)

# M's declared parameters: chosen on 2015-08..2023-08 by mean exact-score
# log-loss over K {10..40}, home advantage {30..120}, and three entry rules
# (results.json "e1.params"; RESULTS.md). The rule was fixed before the test
# window was scored; it was re-applied once after a data fix (seasons tagged
# by date put July 2020 in 2020-21), which moved delta_above from 75 to 100.
K = 20
HOME_ADV_ELO = 60.0
ENTRY_RULE = {"scheme": "offset", "delta_above": 100.0, "delta_below": -75.0}

FIT_WINDOW = ("2015-08-01", "2023-08-01")     # the backtest's fit window
TEST_WINDOW = ("2023-08-01", "2026-07-01")    # scored out of time
FULL_WINDOW = ("2015-08-01", "2026-07-01")    # the live curve, as the EPL's

# 2026-27 clubs, football-data.co.uk spelling, read from its 2026-27 E1
# results (24 clubs; arrivals: Burnley, West Ham, Wolves from the Premier
# League, Bolton, Cardiff, Lincoln from League One). A misspelled club
# would silently be rated by the entry rule's default and locked that way.
ROSTER = frozenset({
    "Birmingham", "Blackburn", "Bolton", "Bristol City", "Burnley", "Cardiff",
    "Charlton", "Derby", "Lincoln", "Middlesbrough", "Millwall", "Norwich",
    "Portsmouth", "Preston", "QPR", "Sheffield United", "Southampton",
    "Stoke", "Swansea", "Watford", "West Brom", "West Ham", "Wolves",
    "Wrexham",
})
ROSTER_SEASON = 2026

# Known fixtures with confirmed kickoffs: none loaded yet (checklist item 2).
FIXTURES = []

CONFIG = CompetitionConfig(
    competition_id="efl-championship-2026-27",
    era=ERA,
    format="round_robin",
    sample=SampleDefinition(mode="dynamic", match_id_pattern=_MATCH_ID_PATTERN),
    home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=HOME_ADV_ELO),
    elo_tiers=EloTierTable(default_k=K),
    # The EPL's starting point (12), not a measurement: no J and no judgment
    # are wired here, so no mechanism can accumulate yet. Revisit with the
    # judgment layer, on this window's own ledger.
    mechanism_min_n=12,
    # PROVISIONAL, not reviewed by the evaluator (module docstring): the
    # EPL's rule -- the ~70th / ~88th percentile of M's largest positive gap
    # to the close (on E0 that rule gives 6.7 / 10.3, the EPL's 6.5 / 10) --
    # applied to the Championship backtest: 7.0 / 10.0 (results.json
    # e1.summary.edge_quantiles_pp). Investigation triggers, not EV-validated
    # cutoffs: no threshold showed a positive ROI. stake_u and
    # max_bets_per_match are the EPL's unit and correlation conventions.
    gate=GateThresholds(watch_pp=7.0, bet_pp=10.0, stake_u=1.0,
                         max_bets_per_match=2),
    # Measured 2026-10-10 on this division's own data, M ONLY
    # (research/championship_backtest/RESULTS.md); J and judgment unmeasured.
    known_biases=(
        "M's probabilities are BEATEN BY THE CLOSING PRICE out of sample: "
        "mean RPS gap +0.0057 [+0.0028, +0.0086] over 1375 Championship "
        "matches 2023-08..2026 with a Pinnacle close, and +0.0159 [+0.0045, "
        "+0.0270] (n=278) where M disagrees with the close by >=10pp; the gap "
        "grows with the disagreement. A big gap is a warning about M, not an "
        "opportunity. Measured on M alone: says nothing about a J or a "
        "judgment that departs from M on its own evidence",
        "no gap-only betting threshold (0-15pp, 1u at the Pinnacle close) "
        "had a positive ROI point estimate; every 95% interval spans or sits "
        "below zero. No profit claim follows from M here",
        "Elo is built from results and cannot see squad composition; six "
        "clubs a season enter by a fitted rule (+100 / -75 Elo around the "
        "continuing mean), not by anything known about their squads",
    ),
    data_sources=DataSources(
        rating_source="self_computed_elo_replay:division_elo.offset",
        results_source=f"football_data_co_uk:{DIVISION}",
        odds_book_default="pinnacle",
        odds_region_default="eu",
    ),
)

EVIDENCE_CATEGORIES = list(CONFIG.evidence_categories)


def current_season_start_year(today=None):
    from football_ai.data.football_data_co_uk import (
        current_season_start_year as _impl)
    return _impl(today)


def _load(division, years):
    """Price-free results (the loader's default), tagged with the season of
    the file they came from. Never from the date: 2019-20 ran into late July
    2020, and a month >= 7 rule files those matches under 2020-21, which
    breaks who counts as an arrival (caught by the backtest's pyramid check)."""
    from football_ai.data.football_data_co_uk import load_seasons
    rows = []
    for y in years:
        for r in load_seasons(division, [y], missing_ok=True):
            r["season"] = y
            rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def _above(years):
    """{season: clubs in the Premier League that season} -- only to tell an
    arrival from above from one from below; never a rating input."""
    out = {}
    for r in _load(DIVISION_ABOVE, years):
        out.setdefault(r["season"], set()).update((r["home_team"], r["away_team"]))
    return out


def _replay(matches, above, rosters=None):
    from football_ai.models.division_elo import replay_division
    return replay_division(matches, K, HOME_ADV_ELO, ENTRY_RULE["scheme"],
                           above=above, delta_above=ENTRY_RULE["delta_above"],
                           delta_below=ENTRY_RULE["delta_below"],
                           rosters=rosters)


def current_ratings(through=None, replay_from=REPLAY_FROM):
    """Elo for the clubs in the division now, replayed from 2005-06 through
    the latest result on file, the entry rule applied every summer --
    including this one before a ball is kicked (`ROSTER` stands in for the
    season's clubs until its results file exists). Clubs that left the
    division are not in the returned dict."""
    through = through if through is not None else current_season_start_year()
    years = range(replay_from, through + 1)
    matches = _load(DIVISION, years)
    rosters = {ROSTER_SEASON: set(ROSTER)} if through >= ROSTER_SEASON else None
    _, ratings = _replay(matches, _above(years), rosters=rosters)
    return ratings


def _fit_params():
    from football_ai.store import io as store_io
    recs = [r["data"] for r in store_io.read("calibrations", era=ERA)
            if str(r["data"].get("model", "")).startswith("m_dc")
            and r["data"].get("gate", {}).get("decision") == "adopted"]
    if not recs:
        raise RuntimeError(
            f"REFUSED: no adopted m_dc calibration for era={ERA!r} in the "
            f"store. Activation step 1: the main session appends it with "
            f"`python -m football_ai.competitions.efl_championship_2026_27 "
            f"calibration --append` (subagents never write the ledger)")
    p = recs[-1]["full_window_params"]
    return p["g_a"], p["g_b"], p["rho"]


def goal_curve():
    """(g_a, g_b, rho) from this era's adopted calibration record."""
    return _fit_params()


def calibration_record(matches=None, above=None):
    """The `calibrations` record this window needs, computed from results
    only: the out-of-time admission gate the EPL ran (Dixon-Coles must beat
    independent Poisson on exact-score log-loss in the test window, rho off
    its search boundary) and the full-window goal curve the live window
    uses. Returned, never written -- `main(["calibration", "--append"])`
    writes it, from the main session."""
    from football_ai.models.m_dc import (
        RHO_RANGE, _score_logloss, fit_goals, fit_rho)
    import numpy as np
    # complete seasons only: 2005-06 .. 2025-26 (the test window's end)
    years = range(REPLAY_FROM, int(TEST_WINDOW[1][:4]))
    if matches is None:
        matches = _load(DIVISION, years)
    if above is None:
        above = _above(years)
    if any(k.startswith("pinnacle") for m in matches for k in m):
        raise ValueError("REFUSED: M's calibration inputs must be price-free")
    rows, _ = _replay(matches, above)

    def window(lo, hi):
        return [r for r in rows if lo <= r["date"] < hi]

    def curve(rs):
        d = np.array([r["d_eff"] for r in rs])
        hs = np.array([r["hs"] for r in rs])
        as_ = np.array([r["as_"] for r in rs])
        g_a, g_b = fit_goals(d, hs, as_)
        return g_a, g_b, fit_rho(d, hs, as_, g_a, g_b)

    fit, test, full = window(*FIT_WINDOW), window(*TEST_WINDOW), window(*FULL_WINDOW)
    g_a, g_b, rho = curve(fit)
    trip = [(r["d_eff"], r["hs"], r["as_"]) for r in test]
    ll_dc = _score_logloss(trip, g_a, g_b, rho)
    ll_ind = _score_logloss(trip, g_a, g_b, 0.0)
    boundary = min(abs(rho - RHO_RANGE[0]), abs(rho - RHO_RANGE[1])) < 1e-6
    fa, fb, fr = curve(full)
    return {
        "model": "m_dc (division_elo offset replay)",
        "competition_id": CONFIG.competition_id,
        "data_source": (f"football-data.co.uk {DIVISION}.csv results, seasons "
                        f"{REPLAY_FROM}-{(REPLAY_FROM + 1) % 100:02d}.."
                        f"{years[-1]}-{(years[-1] + 1) % 100:02d}; "
                        f"{DIVISION_ABOVE} membership only for arrival direction"),
        "declared_params": {"k_default": K, "home_adv_elo": HOME_ADV_ELO,
                            "entry_rule": dict(ENTRY_RULE),
                            "replay_from": REPLAY_FROM},
        "selection": ("K, home advantage and the entry rule chosen on the fit "
                      "window by mean exact-score log-loss "
                      "(research/championship_backtest/backtest.py)"),
        "fit_window": f"{FIT_WINDOW[0]}..{FIT_WINDOW[1]} (n={len(fit)})",
        "val_window": f"{TEST_WINDOW[0]}..{TEST_WINDOW[1]} (n={len(test)})",
        "oot": {"rho_fit": rho, "g_a_fit": g_a, "g_b_fit": g_b,
                "score_logloss_dc": ll_dc, "score_logloss_independent": ll_ind},
        "gate": {"decision": ("adopted" if ll_dc < ll_ind and not boundary
                              else "rejected"),
                 "criterion": "OOT exact-score log-loss strictly better than "
                              "independent Poisson, rho not on its search "
                              "boundary (m_dc.run_gate methodology)",
                 "margin": ll_ind - ll_dc, "rho_on_boundary": boundary},
        "full_window_params": {"g_a": fa, "g_b": fb, "rho": fr, "n": len(full)},
        "notes": ["market-free by construction: results columns only",
                  "M against the Pinnacle close: research/championship_backtest/"
                  "RESULTS.md (scored there, never an input here)"],
    }


def lock_match(match_id, home_team, away_team, kickoff_utc, ratings=None):
    """M for one fixture, written as a version=1 lock under this era. Same
    guards as epl_2026_27.lock_match: a confirmed kickoff still ahead, a
    checkout current with origin, no existing lock, a club in ROSTER, and an
    adopted calibration in the store."""
    from football_ai.competitions.epl_2026_27 import (
        _canonical_sha, _validate_payload)
    from football_ai.models.m_dc import outcome_probs
    from football_ai.store import io as store_io
    from football_ai.store import sync as store_sync

    if not kickoff_utc:
        raise ValueError(f"REFUSED: {match_id!r} has no confirmed kickoff; a "
                         f"guessed time must not be recorded")
    ko = datetime.fromisoformat(kickoff_utc)
    now = datetime.now(timezone.utc)
    if ko < now:
        raise ValueError(f"REFUSED: kickoff {kickoff_utc} has passed (now "
                         f"{now.isoformat()}) -- a lock after kickoff is never "
                         f"honest")
    store_sync.assert_current()
    if any(r.get("match_id") == match_id
           for r in store_io.read("locks", era=ERA)):
        raise ValueError(f"REFUSED: {match_id!r} already has a lock (era="
                         f"{ERA!r}); this window has no amend path yet")
    unknown = [t for t in (home_team, away_team) if t not in ROSTER]
    if unknown:
        raise ValueError(f"REFUSED: {unknown} not in ROSTER (football-data.co.uk "
                         f"spelling, e.g. 'Sheffield United', 'QPR')")
    g_a, g_b, rho = _fit_params()
    ratings = ratings if ratings is not None else current_ratings()
    missing = [t for t in (home_team, away_team) if t not in ratings]
    if missing:
        raise ValueError(f"REFUSED: no rating for {missing}; the replay did "
                         f"not reach them (a default would be locked forever)")
    eh, ea = ratings[home_team], ratings[away_team]
    d_eff = eh - ea + HOME_ADV_ELO
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
                   "entry_rule": dict(ENTRY_RULE),
                   "goal_curve": {"g_a": g_a, "g_b": g_b,
                                  "dixon_coles_rho": rho}},
    }
    _validate_payload(payload, match_id)
    return store_io.append(
        "locks", {"locked_at": now.isoformat(), "version": 1,
                  "payload": payload, "sha256": _canonical_sha(payload)},
        era=ERA, match_id=match_id, version=1)


def amend_lock(match_id, payload, reason, acknowledged_post_odds=False):
    """Refuses: J and the judgment layer are not wired for this window
    (owner brief, 2026-10-10: M and closing-price scoring first). Wiring J
    means porting epl_2026_27.amend_lock's guards and an evaluator review of
    the J spec for this division, not deleting this refusal."""
    raise ValueError(
        f"REFUSED: {CONFIG.competition_id} has no J or amend path yet (M-only "
        f"trial); {match_id!r} was not amended")


def settle_pending(now=None, dry_run=False):
    """Settle locked, kicked-off fixtures from football-data.co.uk's E1 file
    (the declared results source); never a hand-typed score."""
    from football_ai.competitions import ops
    return ops.settle_pending(CONFIG, now=now, dry_run=dry_run)


def _selfcheck_body():
    global calibration_record   # swapped for a stub below, then restored
    import tempfile
    from datetime import timedelta
    from pathlib import Path

    from football_ai.store import io as store_io

    fails = []
    if CONFIG.knockout is not None or CONFIG.format != "round_robin":
        fails.append("the Championship is a round robin")
    if CONFIG.era in ("v2-wc2026", "v3", "v1-epl-2026-27"):
        fails.append("era must not collide with another window's")
    if CONFIG.data_sources.results_source != "football_data_co_uk:E1":
        fails.append("results must come from football-data.co.uk E1")
    if len(ROSTER) != 24:
        fails.append(f"the Championship has 24 clubs, ROSTER has {len(ROSTER)}")
    if CONFIG.sample.parse_num("elc2627_gw07_wat_bur") != 7:
        fails.append("match ids must parse their gameweek")
    if not CONFIG.gate.watch_pp < CONFIG.gate.bet_pp:
        fails.append("watch threshold must sit below the bet threshold")

    # --- current_ratings: offline, on a stubbed two-division world --------
    import football_ai.data.football_data_co_uk as fd
    seen_kwargs = []

    def mk(date, h, a, hs, as_):
        return {"date": date, "home_team": h, "away_team": a,
                "home_score": hs, "away_score": as_}

    # The last 2024 match is played in late July 2025, as 2019-20 was: it
    # must stay in 2024, or A and D look like they never left.
    world = {
        ("E1", 2024): [mk("2024-08-10", "A", "B", 2, 0), mk("2024-08-11", "C", "D", 1, 1),
                       mk("2024-09-10", "B", "C", 0, 1), mk("2024-09-11", "D", "A", 0, 3),
                       mk("2025-07-20", "A", "D", 1, 0)],
        ("E1", 2025): [mk("2025-08-10", "B", "C", 1, 0), mk("2025-08-11", "U", "L", 2, 2)],
        ("E0", 2024): [mk("2024-08-10", "U", "X", 1, 0)],
        ("E0", 2025): [mk("2025-08-10", "A", "X", 1, 0)],
    }

    def fake_load(division, years, **kw):
        seen_kwargs.append(kw)
        return [dict(r) for y in years for r in world.get((division, y), [])]

    real_load = fd.load_seasons
    fd.load_seasons = fake_load
    try:
        r = current_ratings(through=2025, replay_from=2024)
        if any(kw.get("with_closing_odds") for kw in seen_kwargs):
            fails.append("current_ratings must never ask the loader for prices")
        if set(r) != {"B", "C", "U", "L"}:
            fails.append(f"ratings must cover the clubs in the division now "
                         f"(A went up, D down): {sorted(r)}")
        else:
            # Each 2025 match conserves its pair's sum, so U+L and B+C still
            # carry the entry: U entered at base+100, L at base-75 (the
            # backtest's selection, results.json e1.params), base being the
            # mean of B and C. Then U-L drew 2-2 at d_eff 175+HA. Pinned to
            # the numbers, so changing ENTRY_RULE without a new backtest fails.
            base = (r["B"] + r["C"]) / 2
            we = 1.0 / (1.0 + 10.0 ** (-(175.0 + HOME_ADV_ELO) / 400.0))
            want_gap = 175.0 + 2 * K * (0.5 - we)
            if abs((r["U"] + r["L"]) / 2 - (base + 12.5)) > 1e-9 \
                    or abs((r["U"] - r["L"]) - want_gap) > 1e-9:
                fails.append("an arrival from the Premier League must enter at "
                             "the continuing mean +100, from League One at -75: "
                             f"{r}")
        try:
            calibration_record(matches=[dict(world[("E1", 2024)][0], season=2024,
                                              pinnacle_close_h=1.5)],
                               above={2024: set()})
            fails.append("calibration_record must refuse price-carrying inputs")
        except ValueError:
            pass
    finally:
        fd.load_seasons = real_load

    # --- lock_match / amend_lock against a temp store ----------------------
    old_store = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            # a curve the admission gate rejected is never appended
            real_cal = calibration_record
            calibration_record = lambda: {"gate": {"decision": "rejected"},  # noqa: E731
                                          "full_window_params": {}}
            try:
                import contextlib
                import io as _io
                with contextlib.redirect_stdout(_io.StringIO()):
                    main(["calibration", "--append"])
                fails.append("calibration --append must refuse a rejected curve")
            except SystemExit:
                pass
            finally:
                calibration_record = real_cal
            if store_io.read("calibrations", era=ERA):
                fails.append("a rejected calibration reached the store")

            ko = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
            fake = {"Watford": 1560.0, "Burnley": 1600.0}
            try:
                lock_match("elc2627_gw10_wat_bur", "Watford", "Burnley", ko,
                           ratings=fake)
                fails.append("lock_match must refuse with no adopted calibration")
            except RuntimeError:
                pass
            store_io.append("calibrations", {
                "model": "m_dc (division_elo offset replay)",
                "gate": {"decision": "adopted"},
                "full_window_params": {"g_a": 0.21, "g_b": 0.17, "rho": -0.02}},
                era=ERA)
            rec = lock_match("elc2627_gw10_wat_bur", "Watford", "Burnley", ko,
                             ratings=fake)
            # d_eff = 1560 - 1600 + 60 = +20: a near-even fixture, home ahead
            p = rec["data"]["payload"]["probs_90"]
            if abs(sum(p.values()) - 1) > 1e-9 or not p["away"] < p["home"] < 0.5:
                fails.append(f"lock_match probabilities implausible: {p}")
            if rec["data"]["payload"]["inputs"]["elo_diff_effective"] != 20.0:
                fails.append("lock_match must use this window's home advantage")
            from football_ai.competitions.epl_2026_27 import _canonical_sha
            if rec["data"]["sha256"] != _canonical_sha(rec["data"]["payload"]):
                fails.append("lock sha256 must reproduce from its payload")
            if rec["era"] != ERA:
                fails.append("locks must be written under this window's era")
            for args, why in (
                    (("elc2627_gw10_wat_bur", "Watford", "Burnley", ko), "re-lock"),
                    (("elc2627_gw10_x", "Watfrod", "Burnley", ko), "typo club"),
                    (("elc2627_gw10_y", "Watford", "Burnley",
                      (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),
                     "past kickoff"),
                    (("elc2627_gw10_z", "Watford", "Burnley", None), "no kickoff"),
                    (("elc2627_gw10_q", "Watford", "Stoke", ko), "unrated club")):
                try:
                    lock_match(*args, ratings=fake)
                    fails.append(f"lock_match must refuse: {why}")
                except ValueError:
                    pass
            try:
                amend_lock("elc2627_gw10_wat_bur", rec["data"]["payload"], "J")
                fails.append("amend_lock must refuse: no J for this window yet")
            except ValueError:
                pass
            if [r for r in store_io.read("locks", era="v1-epl-2026-27")]:
                fails.append("a Championship lock leaked into the EPL era")
        finally:
            store_io.STORE_DIR = old_store
    return fails


def _selfcheck():
    """Offline (sync check skipped for the body), then prove lock_match
    really calls the sync guard."""
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

    real = store_sync.assert_current

    def _behind(*a, **k):
        raise store_sync.SyncError("behind origin (test)")

    store_sync.assert_current = _behind
    try:
        from datetime import timedelta
        ko = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        lock_match("elc2627_gw10_t", "Watford", "Burnley", ko, ratings={})
        fails.append("lock_match must refuse while the checkout is behind origin")
    except store_sync.SyncError:
        pass
    finally:
        store_sync.assert_current = real
    return fails


def main(argv=None):
    """`calibration` prints the record; `calibration --append` writes it
    (main session only). No argument runs the self-check."""
    import argparse
    import json
    ap = argparse.ArgumentParser(description="EFL Championship 2026-27 window")
    ap.add_argument("cmd", nargs="?", choices=["calibration"])
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "calibration":
        rec = calibration_record()
        print(json.dumps(rec, ensure_ascii=False, indent=2, default=float))
        if args.append:
            if rec["gate"]["decision"] != "adopted":
                raise SystemExit("REFUSED: the gate rejected the curve; nothing written")
            from football_ai.store import io as store_io
            store_io.append("calibrations", rec, era=ERA)
            print(f"appended to calibrations (era={ERA}); publish with "
                  f"`python -m football_ai.store.sync publish -m ...`")
        return
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")


if __name__ == "__main__":
    main()
