"""results_full.json -> table.json (the lookup table + OOS verdict + usage formula).

Usage: python make_table.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
Z95 = 1.959964
ORDER = ["GK", "CB", "FB", "DM", "CM", "WIDE", "AM", "ST", "DEF", "MID", "ATT"]


def ci(b, se):
    return [round(b - Z95 * se, 4), round(b + Z95 * se, 4)]


def rows_out(rows, ppml=None):
    out = []
    pp = {}
    for p in ppml or []:
        pp[(p["group"], p["side"])] = p
    for r in sorted(rows, key=lambda r: (ORDER.index(r["group"]) if r["group"] in ORDER else 99, r["tier"])):
        o = {"group": r["group"], "tier": r["tier"],
             "d_xg_for": r["d_xg_for"], "se_for": r["se_for"], "ci95_for": ci(r["d_xg_for"], r["se_for"]),
             "d_xg_against": r["d_xg_against"], "se_against": r["se_against"],
             "ci95_against": ci(r["d_xg_against"], r["se_against"]),
             "se_twoway_for": r["se_for_twoway"], "se_twoway_against": r["se_against_twoway"],
             "n_absent_player_matches": r["n_absent_player_matches"],
             "n_team_matches_with_absence": r["n_team_matches_with_absence"],
             "n_key_player_seasons": r.get("n_key_player_seasons"),
             "n_key_player_seasons_with_absence": r["n_key_player_seasons_absent"]}
        if (r["group"], "xg_for") in pp:
            a, d = pp[(r["group"], "xg_for")], pp[(r["group"], "xg_against")]
            o["pct_xg_for_ppml"] = round(a["pct"], 2)
            o["pct_xg_for_ppml_ci95"] = [round(a["pct_lo"], 2), round(a["pct_hi"], 2)]
            o["pct_xg_against_ppml"] = round(d["pct"], 2)
            o["pct_xg_against_ppml_ci95"] = [round(d["pct_lo"], 2), round(d["pct_hi"], 2)]
        out.append(o)
    return out


def compact(rows):
    return {r["group"]: {"d_for": r["d_xg_for"], "se_for": r["se_for"],
                         "d_against": r["d_xg_against"], "se_against": r["se_against"],
                         "n": r["n_absent_player_matches"]}
            for r in sorted(rows, key=lambda r: ORDER.index(r["group"]))}


def verdict(x):
    lo, hi = x["mse_reduction_ci95"]
    if lo > 0:
        return "improves"
    if hi < 0:
        return "worsens"
    return "no significant change"


def oos_summary(o):
    def one(x):
        return {"mse_reduction": round(x["mse_reduction"], 5),
                "mse_reduction_ci95": [round(v, 5) for v in x["mse_reduction_ci95"]],
                "mse_reduction_pct": round(x["mse_reduction_pct"], 3),
                "mae_reduction": round(x["mae_reduction"], 5),
                "rows_with_absence": x["rows_with_absence"],
                "mse_reduction_rows_with_absence": round(x["mse_reduction_rows_with_absence"], 5),
                "mse_reduction_rows_with_absence_ci95": [round(v, 5) for v in x["mse_reduction_rows_with_absence_ci95"]],
                "mse_reduction_rows_with_absence_pct": round(x["mse_reduction_rows_with_absence_pct"], 3),
                "calibration_slope": round(x["calibration_slope"], 3),
                "calibration_slope_se": round(x["calibration_slope_se"], 3)}
    return {"verdict": verdict(o["B"]), "n_rows": o["n_rows"], "n_matches": o["n_matches"], "train_seasons": o["train_seasons"],
            "test_seasons": o["test_seasons"], "mse_baseline": round(o["mse_A"], 5),
            "both_sides": one(o["B"]), "xg_for_terms_only": one(o["B_att"]), "xg_against_terms_only": one(o["B_def"])}


def worked_example(rows):
    g = {r["group"]: r for r in rows}
    f = (g["ST"]["d_xg_for"], g["CB"]["d_xg_for"])
    a = (g["ST"]["d_xg_against"], g["CB"]["d_xg_against"])
    am, dm = 1 + sum(f) / 1.60, 1 + sum(a) / 1.10
    am8, dm8 = 1 + 0.8 * sum(f) / 1.60, 1 + 0.8 * sum(a) / 1.10
    return {"case": "team baseline xG-for 1.60, xG-against 1.10; its key ST and key CB are both out",
            "attack_multiplier": f"1 + ({f[0]:+.3f} {f[1]:+.3f})/1.60 = {am:.3f}",
            "defence_multiplier": f"1 + ({a[0]:+.3f} {a[1]:+.3f})/1.10 = {dm:.3f}",
            "if_baseline_already_contains_typical_absences": f"scale each d by 0.8 -> {am8:.3f} and {dm8:.3f}"}


def main():
    R = json.load(open(os.path.join(HERE, "results_full.json")))
    meta = R["meta"]
    pp = R["ppml_by_group"]
    T = {
        "name": "EPL key-starter absence -> team xG base rates",
        "generated_by": "baserate/analyze.py + make_table.py (Understat data, numpy OLS/PPML)",
        "data": {
            "source": "understat.com getLeagueData/EPL/<season> (fixtures, team xG) and getMatchData/<id> (rosters)",
            "seasons": [f"{s}-{str(s + 1)[2:]}" for s in meta["seasons"]],
            "n_matches": meta["n_matches"], "n_team_matches": meta["n_team_matches"],
            "mean_xg_per_team_match": round(meta["mean_xg_per_team_match"], 4),
            "mean_xg_home": round(meta["mean_xg_home"], 4), "mean_xg_away": round(meta["mean_xg_away"], 4),
            "complete": len(meta["seasons"]) == 12,
        },
        "definitions": {
            "position_groups": {
                "GK": ["GK"], "CB": ["DC"], "FB": ["DR", "DL", "DMR", "DML", "MR/ML when the XI has exactly 3 DC and no DR/DL/DMR/DML (wing-backs)"],
                "DM": ["DMC"], "CM": ["MC"], "WIDE": ["MR", "ML", "AMR", "AML"], "AM": ["AMC"], "ST": ["FW", "FWL", "FWR"]},
            "position_source": "Understat starting formation slot of each match",
            "key_starter": "started >= 60% of the team's league matches that season (any slot); assigned to the position group where most of his starts were (modal group). Spec-literal rule (>= 60% in one group) reported under robustness.strict_key_rule.",
            "absence": "a key starter who is not in that match's starting XI (injury, suspension, rotation, or not yet signed / already left)",
            "tiers": {
                "start_share": R["tier_start_share"]["definition"],
                "attack_rank": R["tier_attack_rank"]["definition"]},
        },
        "model": {
            "equation": "xg_for[t,m] = a[team-season] + d[opp-season] + h*home + sum_g b_for[g]*n_absent_own[g] + sum_g b_against[g]*n_absent_opp[g] + e",
            "note": "one row per team per match; b_for = change in the absent team's xG-for per absent key starter; b_against = change in the absent team's xG-against (estimated from the opponent's rows). Equivalent to separate xG-for and xG-against regressions with team-season FE, opponent-season FE, home, own absences, each also controlling for the other side's absences.",
            "se": "clustered by the absent team's team-season (se_*); se_twoway_* also clusters by the other team-season",
            "sample": "all seasons in data.seasons",
            "home_coef": round(R["main_by_group"]["fit"]["home"], 4),
            "resid_sd": round(R["main_by_group"]["fit"]["resid_sd"], 4),
        },
        "base_rates": {
            "by_group": rows_out(R["main_by_group"]["rows"], pp),
            "by_group_recent_2022_25": rows_out(R["robustness"]["test_only"]["rows"]),
            "by_line": rows_out(R["by_line"]["rows"]),
            "by_group_alt_fwl_fwr_as_wide": rows_out(R["robustness"]["fwl_fwr_as_wide"]["rows"]),
            "by_group_x_start_share_tier": rows_out(R["tier_start_share"]["rows"]),
            "by_group_x_attack_rank_tier": rows_out(R["tier_attack_rank"]["rows"]),
            "any_key_starter": {
                "d_xg_for": round(R["additivity"]["per_absence_all_groups"]["d_xg_for"]["b"], 4),
                "se_for": round(R["additivity"]["per_absence_all_groups"]["d_xg_for"]["se"], 4),
                "d_xg_against": round(R["additivity"]["per_absence_all_groups"]["d_xg_against"]["b"], 4),
                "se_against": round(R["additivity"]["per_absence_all_groups"]["d_xg_against"]["se"], 4)},
        },
        "key_starter_summary": R["key_summary"] | R.get("key_summary_extra", {}),
    }
    if "oos" in R:
        o = R["oos"]
        T["out_of_sample"] = {
            "design": "coefficients fit on train seasons; on test seasons the baseline (team-season + opp-season FE + home) is cross-fitted with 10 folds by match, so every test match is predicted from FE estimated without it. Model A = baseline; model B = baseline re-fitted on y minus the train-season absence offset, plus that offset. Loss = squared error of per-team-match xG. CI clusters by team-season (or by match, whichever is wider).",
            "pooled_by_group_additive": oos_summary(o["pooled_by_group_additive"]),
            "pooled_by_group_multiplicative": oos_summary(o["pooled_by_group_multiplicative"]),
            "tier_start_share_additive": oos_summary(o["tier_start_share_additive"]),
            "single_count_additive": oos_summary(o["total_count_additive"]),
            "by_line_additive": oos_summary(o["by_line_additive"]),
            "no_lookahead_key_status_second_half": oos_summary(o["no_lookahead_status_second_half"]),
        }
    add = R["additivity"]
    T["usage"] = {
        "attack_multiplier": "1 + sum_i d_xg_for[group_i] / base_xg_for",
        "defence_multiplier": "1 + sum_i d_xg_against[group_i] / base_xg_against   (applied to the opponent's expected xG)",
        "multiplicative_alternative": "prod_i (1 + pct_xg_for_ppml[group_i]/100)  -- same idea on the log scale",
        "recommended_lookup": [
            "default: base_rates.by_group (all 12 seasons), d_xg_for for the absent team's attack, d_xg_against for its defence",
            "known undisputed starter (started >= 80% when the season ends up counted) -> by_group_x_start_share_tier 'core'; known top-3 attacker of the team -> by_group_x_attack_rank_tier 'top3'",
            "a winger in a front three (Understat FWL/FWR) sits in ST in by_group; for role-based lookup use by_group_alt_fwl_fwr_as_wide",
            "xG-against side: 2022-25 effects are about twice the 2014-21 ones (robustness.era_split_any_key_starter; OOS calibration slope > 1 for the xG-against terms); by_group_recent_2022_25 is the recent-era alternative with larger SE",
            "if the CI covers 0, treat the effect as 'small, sign uncertain', not as zero evidence of no effect",
        ],
        "worked_example": worked_example(T["base_rates"]["by_group"]),
        "baseline_caveat": "Coefficients are relative to all key starters starting. A baseline fitted on past results (M) already contains the team's typical absences (key starters miss ~20% of matches), so relative to such a baseline use d * (1 - p), p = that player's usual absence rate (default 0.2).",
        "additivity_evidence": {
            "extra_term_per_absence_beyond_first_for": {k: round(v, 4) for k, v in add["beyond_first_absence_term"]["for"].items()},
            "extra_term_per_absence_beyond_first_against": {k: round(v, 4) for k, v in add["beyond_first_absence_term"]["against"].items()},
            "count_dummies": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in add["count_dummies_vs_zero"].items()},
            "own_absence_count_distribution": add["distribution_own_count"],
        },
    }
    rb = R["robustness"]
    T["robustness"] = {
        "strict_key_rule": compact(rb["strict_key_rule"]["rows"]),
        "drop_each_team_last5": compact(rb["drop_last5"]["rows"]),
        "rest_day_controls": compact(rb["rest_day_controls"]["rows"]),
        "recent_form_controls": compact(rb["recent_form_controls"]["rows"]),
        "placebo_next_match_absence_by_line": rb["placebo_lead_absence_by_line"],
        "placebo_next_match_absence_split_full90_vs_subbed_off": rb["placebo_lead_split_full90_vs_subbed_off"],
        "placebo_split_event_counts": rb["placebo_lead_split_event_counts"],
        "era_split_any_key_starter": {"2014_21": rb["by_line_train_only"]["any_key"], "2022_25": rb["by_line_test_only"]["any_key"]},
        "did_not_play_only": compact(rb["dnp_vs_benched"]["did_not_play"]),
        "benched_came_on": compact(rb["dnp_vs_benched"]["came_on_as_sub"]),
        **({"train_seasons_only": compact(rb["train_only"]["rows"])} if "train_only" in rb else {}),
        **({"test_seasons_only": compact(rb["test_only"]["rows"])} if "test_only" in rb else {}),
    }
    T["limitations"] = [
        "Endogeneity: rotation concentrates in matches against weak opponents and in congested weeks; opponent-season FE absorbs average opponent strength only, and European/cup fixtures are not in the data (rest-day control uses league fixtures only).",
        "Injury, suspension and tactical rotation are not separated; did_not_play_only is the closest proxy for injury/suspension.",
        "Dead rubbers and within-season form changes (manager sackings) are not modelled directly; drop_each_team_last5 and recent_form_controls (previous-5-match xG form of both sides) leave the estimates essentially unchanged.",
        "Placebo: the match BEFORE an absence already shows part of the effect. Most of it sits in matches where the player was subbed off before 85' (in-match injury: the disruption starts in that match), but for defenders who played the full 90 the next-match absence still predicts +0.07 xG-against in the current match (cards/knocks picked up under pressure, or selection). Defensive coefficients may therefore be somewhat overstated; the GK estimate is the least trustworthy.",
        "Era shift: absence effects on xG-against in 2022-25 are about 2x those of 2014-21 (any key starter +0.099 vs +0.049 per absence); the cause is not identified (five-substitute rule, style, sample noise).",
        "Tiers are noisier than the pooled group estimates; GK 'regular' (60-80% starts) > 'core' is consistent with keepers being dropped after poor spells, i.e. selection.",
        "Key status uses the whole season (look-ahead); out_of_sample.no_lookahead_key_status_second_half tests status from the first 19 matches only.",
        "Replacement quality is not modelled: the coefficient is the average net effect after the manager's replacement choice.",
        "Understat position labels are formation slots; wide forwards in a front three are FWL/FWR (ST group), wing-backs follow the back-three rule.",
        "Absences include matches before a mid-season signing's arrival (see key_starter_summary.absences_before_first_appearance_fraction).",
    ]
    json.dump(T, open(os.path.join(HERE, "table.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("wrote table.json")


if __name__ == "__main__":
    main()
