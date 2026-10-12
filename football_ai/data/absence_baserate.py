"""Positional absence base rates: how much a team's xG changes when one of
its key starters does not start, by position group.

Measured on 4,560 EPL matches, 2014-15 to 2025-26 (Understat rosters and
match xG; key starter = started >= 60% of the team's league matches that
season), with team-season and opponent-season fixed effects and a home
term. Built by `research/absence_baserate/` (fetch, build, analyze,
make_table, selftest); the table is `absence_baserates.json` beside this
file. An independent recomputation of the pooled effect (-0.0499 xG-for,
+0.0656 xG-against per absent key starter) matched it exactly.

What it is for: when a player's own numbers cannot be found, the measured
effect for his position group replaces "assume zero". A default of zero is
a limit like any other and needs grounds (DECISION_PROTOCOL section 4).

How far to trust it (out of sample, trained 2014-21, tested 2022-25):
  * xG-against: improves prediction (CI excludes 0); 2022-25 effects are
    about twice the 2014-21 ones, so the recent table is the default here.
    A placebo test (next match's absence "predicting" this one) points to
    some selection bias, so treat defence effects as an upper-leaning read.
  * xG-for: no significant out-of-sample gain, calibration slope 0.67, so
    attack effects are multiplied by ATTACK_SHRINK.
  * Relative to a baseline fitted on past results (M, or a team's season
    numbers), which already contains the team's usual absences, scale each
    effect by (1 - p_usual_absence), default 0.2.
"""
import json
from pathlib import Path

TABLE = Path(__file__).with_name("absence_baserates.json")
GROUPS = ("GK", "CB", "FB", "DM", "CM", "WIDE", "AM", "ST")
ATTACK_SHRINK = 0.67     # out-of-sample calibration slope of the attack term
DEFAULT_P_USUAL = 0.2    # key starters miss ~20% of matches in the fit window

_cache = None


def table():
    global _cache
    if _cache is None:
        _cache = json.loads(TABLE.read_text(encoding="utf-8"))
    return _cache


def effect(group, recent_defence=True):
    """Per-absence {d_xg_for, se_for, d_xg_against, se_against} for a group.
    Attack comes from the all-seasons table; defence from 2022-25 by default."""
    if group not in GROUPS:
        raise ValueError(f"group must be one of {GROUPS}, got {group!r}")
    br = table()["base_rates"]
    full = next(r for r in br["by_group"] if r["group"] == group and r["tier"] == "all")
    dfn = full
    if recent_defence:
        dfn = next(r for r in br["by_group_recent_2022_25"]
                   if r["group"] == group and r["tier"] == "all")
    return {"d_xg_for": full["d_xg_for"], "se_for": full["se_for"],
            "d_xg_against": dfn["d_xg_against"], "se_against": dfn["se_against"]}


def multipliers(absent_groups, base_xg_for, base_xg_against,
                p_usual=DEFAULT_P_USUAL, recent_defence=True):
    """Attack and defence multipliers for a team missing the listed key
    starters (one entry per absent player; effects add, which the fit
    supports). `base_*` are the team's per-match baselines the multipliers
    apply to. Returns the multipliers with 1-SD bands from the table SEs."""
    if base_xg_for <= 0 or base_xg_against <= 0:
        raise ValueError("baselines must be positive")
    scale = 1.0 - p_usual
    d_for = d_against = var_for = var_against = 0.0
    for g in absent_groups:
        e = effect(g, recent_defence=recent_defence)
        d_for += ATTACK_SHRINK * scale * e["d_xg_for"]
        d_against += scale * e["d_xg_against"]
        var_for += (ATTACK_SHRINK * scale * e["se_for"]) ** 2
        var_against += (scale * e["se_against"]) ** 2
    return {"attack": 1 + d_for / base_xg_for,
            "attack_sd": var_for ** 0.5 / base_xg_for,
            "defence": 1 + d_against / base_xg_against,
            "defence_sd": var_against ** 0.5 / base_xg_against}


def _selfcheck():
    fails = []
    try:
        t = table()
    except Exception as e:
        return [f"absence_baserates.json must load: {e}"]
    if {r["group"] for r in t["base_rates"]["by_group"]} != set(GROUPS):
        fails.append("the table must cover every position group")
    none = multipliers([], 1.5, 1.2)
    if none["attack"] != 1.0 or none["defence"] != 1.0:
        fails.append(f"no absence must leave both multipliers at 1: {none}")
    one = multipliers(["ST"], 1.5, 1.2)
    two = multipliers(["ST", "ST"], 1.5, 1.2)
    if not (two["attack"] < one["attack"] < 1.0):
        fails.append(f"absent strikers must lower attack, additively: {one}, {two}")
    if abs((1 - two["attack"]) - 2 * (1 - one["attack"])) > 1e-12:
        fails.append("effects must add per absent player")
    cb = multipliers(["CB"], 1.5, 1.2)
    if not cb["defence"] > 1.0:
        fails.append(f"an absent centre-back must raise xG conceded: {cb}")
    raw = effect("ST")["d_xg_for"]
    if abs((one["attack"] - 1) * 1.5 - ATTACK_SHRINK * (1 - DEFAULT_P_USUAL) * raw) > 1e-12:
        fails.append("attack must apply the out-of-sample shrink and the usual-absence scale")
    try:
        effect("XX")
        fails.append("an unknown group must be refused")
    except ValueError:
        pass
    return fails
