"""Competition configuration abstraction (multi-league generalization, git
f01aed1; audit: `research/audit/final_report.md`). "prompt task N" below
refers to the brief that work was built from, kept in git history:
`git show f01aed1:research/registrations/multi-league-generalization-prompt.md`.

WHY: the WC2026 window's identity facts (MAIN_SAMPLE_NUMS, the `m<NN>_` id
regex, ERA="v2-wc2026", the K-tier table, HOME_ADV, the SOP gate numbers...)
are module-level literals scattered across registry.py, agenda.py, m_dc.py
and sop.py, and the sample/id/era trio alone is independently duplicated in
at least four files with no shared source of truth (see
research/audit/final_report.md, "Executive Summary" and section 4.1/4.9).
This module gives every other module ONE place to read those facts from,
parameterized per competition window, instead of re-declaring its own copy.

SCOPE: this is the identity/structural layer only. Parameters that are FIT
from a competition's own historical data (the Dixon-Coles goal-model basis
and rho, the ordered-logit coefficients, and any other regression output)
are NOT config fields here -- they live in `store/io.py`'s existing
"calibrations" kind, tagged with this same `era` (exactly how
`models/m_dc.py`'s own gate result is already persisted for WC2026:
`io.append("calibrations", res, era="v3")`); a consumer reads them back via
`io.read("calibrations", era=config.era)`. An earlier draft of this file
added a separate `calibration_path` field pointing at a bespoke JSON file --
redundant once `era` already doubles as the store's join key, so removed.
This also deliberately excludes a
"mu_total_goals" field an earlier draft of this file carried: the archived
v2 pipeline's `model.py:solve_lambdas(total_goals=2.4)` does hand-declare a
goals basis, but the LIVE, reusable `models/m_dc.py` does not -- its
`fit_goals()` regresses `lambda = exp(a + b*d_eff/100)` directly from a
competition's own historical results (Newton's method MLE), so the goals
basis is baked into that fit's `a` coefficient, not a separate declared
constant. Reintroducing it here would recreate exactly the "declare a
number that a real fit already produces" mistake this file exists to avoid.
The Elo K-tier table and the home-advantage constant ARE config fields
here, matching how the current codebase already treats them: hand-declared
module constants (m_dc.py HOME_ADV/k_factor), not fit outputs. Putting FIT
outputs in this file would re-invite the exact mistake this abstraction
exists to fix -- treating one window's derived numbers as universal
constants for the next.

OUT OF SCOPE for this file (owner decision 2026-07-24, generalization
prompt task 6): the WC2026 falsification-experiment design (B1/B2 arms,
pilot/main sample split, significance thresholds) is not represented here.
It is experiment-bound, not competition-bound -- see
research/audit/final_report.md's states.py finding, and
football_ai/core/states.py itself, whose B1/B2/shadow arm-validity logic is
simply unused (not reparameterized) for a window that runs no such
experiment. A future window that wants to revive that design needs its own
PROTOCOL.md-style pre-registration, not a field bolted onto this dataclass.
"""
import operator
import re
import types
from dataclasses import dataclass, field
from typing import Optional

_VALID_FORMATS = ("round_robin", "knockout")


@dataclass(frozen=True)
class SampleDefinition:
    """Which fixtures this window tracks, and how their ids parse.

    `mode="fixed"`: WC2026's shape -- a closed set of match numbers
    pre-registered before any of them kick off, required so a
    falsification-style experiment can't cherry-pick its sample after the
    fact (registry.py MAIN_SAMPLE_NUMS). A plain decision-support window
    that runs no such experiment does not need this at all.
    `mode="dynamic"`: the normal league shape -- whatever fixtures are
    currently upcoming/tracked, no frozen population declared in advance.
    """
    mode: str  # "fixed" | "dynamic"
    match_id_pattern: str = r"^m(\d+)_"
    match_ids: frozenset = field(default_factory=frozenset)  # "fixed" only
    aliases: dict = field(default_factory=dict)
    known_missing: dict = field(default_factory=dict)

    def __post_init__(self):
        # frozen=True blocks attribute rebinding, not in-place dict
        # mutation -- wrap in a read-only view so `config.sample.aliases["x"]
        # = 1` actually fails instead of silently corrupting a shared config.
        object.__setattr__(self, "aliases",
                            types.MappingProxyType(dict(self.aliases)))
        object.__setattr__(self, "known_missing",
                            types.MappingProxyType(dict(self.known_missing)))

    def parse_num(self, match_id):
        """Extract the numeric fixture id from `match_id`, or None."""
        m = re.match(self.match_id_pattern, match_id)
        return int(m.group(1)) if m else None


@dataclass(frozen=True)
class KnockoutFormat:
    """Present only when `CompetitionConfig.format != "round_robin"`.

    `bracket` is a feeder graph (round/slot name -> the round/slot names
    that must be locked before it), not literal match numbers. WC2026's
    agenda.py hardcodes bracket topology directly in control flow (`if num
    in (101,102): feeders_done = all(locks.get(q) ... for q in
    (97,98,99,100))`) -- the one place in the non-archived codebase where a
    config-file swap alone cannot generalize it (final_report.md, Executive
    Summary). This field exists so that dependency graph lives in data.
    """
    extra_time_lambda_scale: float
    penalty_shootout_home_prob: float = 0.5
    bracket: dict = field(default_factory=dict)
    host_bonus_elo: Optional[float] = None  # only for a designated-host format

    def __post_init__(self):
        object.__setattr__(self, "bracket",
                            types.MappingProxyType(dict(self.bracket)))


@dataclass(frozen=True)
class HomeAdvantageModel:
    """`mode="standard"`: every non-neutral-venue fixture gets
    `+home_adv_elo` (the normal league case -- nearly every fixture has a
    real home team; `model.py:predict_1x2(neutral=False)` already supports
    this per-call, it just needs callers to stop defaulting to True).
    `mode="neutral_default"`: fixtures are neutral-venue unless flagged
    otherwise (the WC2026 case; `KnockoutFormat.host_bonus_elo` layers on
    top for the host nation specifically, kept separate because it is a
    knockout/host concept, not a general home-advantage one).
    `home_adv_elo` must be declared per competition (task 4) -- do not copy
    another window's value; m_dc.py's HOME_ADV=100.0 is this project's own
    hand-set constant, not a universal law.
    """
    mode: str  # "standard" | "neutral_default"
    home_adv_elo: float


@dataclass(frozen=True)
class EloTierTable:
    """Substring-matched competition/tournament tier -> K-factor. WC2026's
    table (m_dc.py `k_factor()`, independently duplicated in
    history/falsification-2026/calibrate.py and ops/scratch_lens4.py --
    final_report.md section 7.1) is one instance of this shape, not a
    universal law. A single-competition league (most domestic top
    divisions) can leave `tiers` empty and rely solely on `default_k`.
    """
    tiers: dict = field(default_factory=dict)
    default_k: int = 30

    def __post_init__(self):
        object.__setattr__(self, "tiers",
                            types.MappingProxyType(dict(self.tiers)))


@dataclass(frozen=True)
class GateThresholds:
    """The pre-registered SOP numbers (decision/sop.py). Re-derive these
    for every new window (generalization prompt task 4) -- do not copy the
    WC2026 values (WATCH=3pp, BET=5pp, STAKE_U=1.0, MAX_BETS_PER_MATCH=2);
    they were sized for that sample and gate design, not derived from
    anything universal about betting markets. `odds_staleness_min` defaults
    to WC2026's 10-minute rule (PROTOCOL.md doc-10) -- reconsider it for any
    new data source, since it should track that source's actual odds
    snapshot cadence, not be inherited blind.
    """
    watch_pp: float
    bet_pp: float
    stake_u: float
    max_bets_per_match: int
    odds_staleness_min: int = 10


@dataclass(frozen=True)
class DataSources:
    """Adapter identifiers, not URLs or parser code -- resolving an
    identifier to an actual fetch/parse implementation is generalization
    prompt task 3, deliberately out of scope for this config layer. Keeping
    these as opaque strings means adding a new provider never requires
    touching this dataclass.
    """
    rating_source: str
    results_source: str
    odds_book_default: str
    odds_region_default: str = "eu"


@dataclass(frozen=True)
class CompetitionConfig:
    """One instance per competition window. Every other module should read
    its era/sample/format/threshold facts from here instead of declaring
    its own copy (final_report.md section 4.1/4.9: today the sample/id/era
    trio alone is independently duplicated across at least four files).

    `mechanism_min_n` is required (no default) on purpose, unlike most
    other fields here -- an earlier draft gave it a WC2026-derived default
    (MIN_N=5) silently, breaking this file's own convention of loudly
    warning instead of quietly reusing a WC2026-sized number (audit review,
    2026-07-24). Forcing an explicit value for every new competition is the
    fix.
    """
    competition_id: str
    era: str
    format: str  # "round_robin" | "knockout" -- see _VALID_FORMATS
    sample: SampleDefinition
    home_advantage: HomeAdvantageModel
    elo_tiers: EloTierTable
    # Minimum sample size before asserting "this mechanism type has an
    # edge" (mechanism_score.py MIN_N). WC2026 used 5, explicitly because
    # "this tournament tops out below it" -- not a universal constant.
    mechanism_min_n: int
    gate: GateThresholds
    data_sources: DataSources
    evidence_categories: tuple = (
        "injury", "suspension", "lineup", "rest_travel", "tactical",
        "incentive", "user_observation", "form_xg", "other",
    )
    knockout: Optional[KnockoutFormat] = None
    # Instrument biases MEASURED ON THIS WINDOW'S OWN DATA. Deliberately
    # per-competition and empty by default: the WC2026 pp-magnitude biases
    # were measured on a 16-match knockout sample, and lending them to
    # another league would be borrowing someone else's track record to
    # sound calibrated. Every entry must cite the sample it came from.
    known_biases: tuple = ()
    # Which frozen J spec this window's instrument runs. NOT a caller
    # argument: a window that depended on whoever wrote the call remembering
    # to pass the right spec is a window that runs the wrong one.
    # "Did J run the upgraded spec?" should be answerable by reading the
    # config, never by trusting a call site. The default is the CURRENT spec;
    # it used to be "v2", which is now an archived spec that `load_spec`
    # refuses, so a new competition that forgot to set this would have failed
    # at its first J run.
    j_spec_version: str = "rr1"

    def __post_init__(self):
        if self.format not in _VALID_FORMATS:
            raise ValueError(
                f"format must be one of {_VALID_FORMATS}, got {self.format!r}")
        if self.format == "round_robin" and self.knockout is not None:
            raise ValueError(
                "format=\"round_robin\" must not carry a KnockoutFormat")
        if self.format == "knockout" and self.knockout is None:
            raise ValueError(
                f"format={self.format!r} requires a KnockoutFormat")
        if self.sample.mode == "fixed" and not self.sample.match_ids:
            raise ValueError(
                "sample.mode == \"fixed\" requires a non-empty match_ids set")
        if self.sample.mode not in ("fixed", "dynamic"):
            raise ValueError(f"unknown sample.mode {self.sample.mode!r}")
        if self.home_advantage.mode not in ("standard", "neutral_default"):
            raise ValueError(
                f"unknown home_advantage.mode {self.home_advantage.mode!r}")
        if self.mechanism_min_n < 1:
            raise ValueError("mechanism_min_n must be >= 1")


def _selfcheck():
    fails = []

    def expect_raises(thunk, tag):
        try:
            thunk()
            fails.append(f"{tag}: expected ValueError, none raised")
        except ValueError:
            pass

    # A round-robin domestic-league window: dynamic sample, real home
    # advantage, no knockout logic at all.
    league = CompetitionConfig(
        competition_id="example-domestic-league-2026-27",
        era="v1-example-league-2026",
        format="round_robin",
        sample=SampleDefinition(mode="dynamic",
                                 match_id_pattern=r"^gw(\d+)_"),
        home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
        elo_tiers=EloTierTable(default_k=20),
        mechanism_min_n=8,
        gate=GateThresholds(watch_pp=3.0, bet_pp=5.0, stake_u=1.0,
                             max_bets_per_match=2),
        data_sources=DataSources(rating_source="clubelo",
                                  results_source="example-league-results-db",
                                  odds_book_default="pinnacle"),
    )
    if league.knockout is not None:
        fails.append("round_robin example should have knockout=None")
    if league.sample.parse_num("gw12_home_away") != 12:
        fails.append("custom match_id_pattern should still parse via SampleDefinition.parse_num")

    # A knockout window shaped like WC2026 itself, to prove the same
    # abstraction expresses the format this project shipped with.
    knockout_window = CompetitionConfig(
        competition_id="example-knockout-cup-2026",
        era="v1-example-knockout-2026",
        format="knockout",
        sample=SampleDefinition(mode="fixed",
                                 match_ids=frozenset(range(89, 105)),
                                 aliases={"r16_por_esp": 93},
                                 known_missing={92: {"reason": "orphan-grab, never locked"}}),
        home_advantage=HomeAdvantageModel(mode="neutral_default", home_adv_elo=100.0),
        elo_tiers=EloTierTable(tiers={"fifa world cup": 60, "friendly": 20},
                                default_k=30),
        mechanism_min_n=5,
        gate=GateThresholds(watch_pp=3.0, bet_pp=5.0, stake_u=1.0,
                             max_bets_per_match=2),
        data_sources=DataSources(rating_source="eloratings_national",
                                  results_source="martj42-international-results",
                                  odds_book_default="pinnacle"),
        knockout=KnockoutFormat(extra_time_lambda_scale=30.0 / 90.0,
                                 bracket={"final": ["sf1", "sf2"]},
                                 host_bonus_elo=75.0),
    )
    if knockout_window.knockout is None:
        fails.append("knockout example should carry a KnockoutFormat")

    # frozen=True must actually stop mutation of the dict-shaped fields,
    # not just attribute rebinding -- MappingProxyType should make these
    # in-place writes raise, matching the "lock immutable facts" ethos.
    for tag, thunk in [
        ("sample.aliases", lambda: operator.setitem(league.sample.aliases, "x", 1)),
        ("sample.known_missing", lambda: operator.setitem(league.sample.known_missing, 1, {})),
        ("elo_tiers.tiers", lambda: operator.setitem(league.elo_tiers.tiers, "x", 1)),
        ("knockout.bracket", lambda: operator.setitem(knockout_window.knockout.bracket, "x", [])),
    ]:
        try:
            thunk()
            fails.append(f"{tag}: in-place mutation should raise, config is meant to be locked")
        except TypeError:
            pass

    expect_raises(
        lambda: CompetitionConfig(
            competition_id="bad", era="v1", format="round_robin",
            sample=SampleDefinition(mode="dynamic"),
            home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
            elo_tiers=EloTierTable(), mechanism_min_n=5,
            gate=league.gate, data_sources=league.data_sources,
            knockout=KnockoutFormat(extra_time_lambda_scale=1.0)),
        "round_robin with a KnockoutFormat attached must raise")

    expect_raises(
        lambda: CompetitionConfig(
            competition_id="bad", era="v1", format="knockout",
            sample=SampleDefinition(mode="dynamic"),
            home_advantage=HomeAdvantageModel(mode="neutral_default", home_adv_elo=100.0),
            elo_tiers=EloTierTable(), mechanism_min_n=5,
            gate=league.gate, data_sources=league.data_sources,),
        "knockout format without a KnockoutFormat must raise")

    expect_raises(
        lambda: CompetitionConfig(
            competition_id="bad", era="v1", format="round_robin",
            sample=SampleDefinition(mode="fixed"),  # empty match_ids
            home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
            elo_tiers=EloTierTable(), mechanism_min_n=5,
            gate=league.gate, data_sources=league.data_sources,),
        "fixed sample mode with no match_ids must raise")

    expect_raises(
        lambda: CompetitionConfig(
            competition_id="bad", era="v1", format="league_of_typos",
            sample=SampleDefinition(mode="dynamic"),
            home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
            elo_tiers=EloTierTable(), mechanism_min_n=5,
            gate=league.gate, data_sources=league.data_sources,),
        "unrecognized format string must raise, not silently pass validation")

    expect_raises(
        lambda: CompetitionConfig(
            competition_id="bad", era="v1", format="round_robin",
            sample=SampleDefinition(mode="dynamic"),
            home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
            elo_tiers=EloTierTable(), mechanism_min_n=0,
            gate=league.gate, data_sources=league.data_sources,),
        "mechanism_min_n < 1 must raise")

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
