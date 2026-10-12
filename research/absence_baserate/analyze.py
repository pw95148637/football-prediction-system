"""Key-starter absence -> team xG base rates (EPL, Understat).

Reads data/matches.jsonl + data/appearances.csv (from build.py), writes
table.json and results_full.json. Deterministic (fixed seeds); numpy/pandas only.

Model (one stacked regression; one row per team per match):
  xg_for[t,m] = a[t,S] + d[o,S] + h*home
                + sum_g b_att[g] * n_abs[t,g,m]      own absent key starters
                + sum_g b_def[g] * n_abs[o,g,m]      opponent's absent key starters
a = team-season attack FE, d = opponent-season defence FE.
b_att[g] = change in the absent team's xG-for; b_def[g] = change in the absent
team's xG-against (that row's xg_for is the absent team's xg_against).
This equals running the two requested regressions (xG-for and xG-against on
own absences + team-season FE + opponent-season FE + home) with each one also
controlling for the other side's absences.

Usage: python analyze.py
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
GROUPS = ["GK", "CB", "FB", "DM", "CM", "WIDE", "AM", "ST"]
TRAIN = list(range(2014, 2022))   # 2014-15 .. 2021-22
TEST = list(range(2022, 2026))    # 2022-23 .. 2025-26
KEY_SHARE = 0.60                  # key starter: started >= 60% of the team's league matches
CORE_SHARE = 0.80                 # start-share tier: core >= 80%, regular 60-80%
ATT_TOP = 3                       # attack tier: top-3 in team (xG+xA)/90 (>= 900 min)
ATT_MIN_MINUTES = 900
LINE = {"GK": "GK", "CB": "DEF", "FB": "DEF", "DM": "MID", "CM": "MID", "WIDE": "ATT", "AM": "ATT", "ST": "ATT"}
KFOLD = 10
SEED = 20261010
Z95 = 1.959964


# ----------------------------------------------------------------------------- data
def load():
    rows = []
    with open(os.path.join(DATA, "matches.jsonl"), encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            if not m["lineups_ok"]:
                continue
            for side, t, o, xf, xa in (("h", m["home"], m["away"], m["xg_home"], m["xg_away"]),
                                       ("a", m["away"], m["home"], m["xg_away"], m["xg_home"])):
                rows.append({"match_id": m["id"], "season": m["season"], "datetime": m["datetime"],
                             "team": t, "opp": o, "home": int(side == "h"),
                             "xg_for": xf, "xg_against": xa})
    tm = pd.DataFrame(rows)
    # keep complete seasons only (a partially fetched season breaks FE connectivity)
    per_season = tm.groupby("season")["match_id"].nunique()
    complete = per_season[per_season >= 380].index
    dropped = sorted(set(per_season.index) - set(complete))
    if dropped:
        print(f"skipping incomplete seasons: {dropped}", file=sys.stderr)
    tm = tm[tm["season"].isin(complete)].copy()
    tm["ts"] = tm["season"].astype(str) + ":" + tm["team"]
    tm["os"] = tm["season"].astype(str) + ":" + tm["opp"]
    tm["dt"] = pd.to_datetime(tm["datetime"])
    tm = tm.sort_values(["ts", "dt"]).reset_index(drop=True)
    tm["match_no"] = tm.groupby("ts").cumcount() + 1
    tm["n_team_matches"] = tm.groupby("ts")["match_id"].transform("size")
    rest = tm.groupby("ts")["dt"].diff().dt.total_seconds() / 86400.0
    tm["rest_days"] = rest.fillna(99.0)
    app = pd.read_csv(os.path.join(DATA, "appearances.csv"))
    app = app[app["match_id"].isin(set(tm["match_id"]))]
    app["ts"] = app["season"].astype(str) + ":" + app["team"]
    return tm, app


def key_players(tm, app, rule="modal", status_matches=None):
    """Key starters per team-season.

    rule='modal'  : started >= 60% of team matches (any slot); group = modal group of his starts
    rule='strict' : started >= 60% of team matches in one position group (spec-literal)
    status_matches: optional set of match_ids used to define status (default: all)
    """
    t = tm if status_matches is None else tm[tm["match_id"].isin(status_matches)]
    n_m = t.groupby("ts").size().rename("n_m")
    a = app if status_matches is None else app[app["match_id"].isin(status_matches)]
    st = a[a["starter"] == 1]
    per_g = st.groupby(["ts", "player_id", "group"]).size().unstack(fill_value=0)
    per_g = per_g.reindex(columns=GROUPS, fill_value=0)
    starts = per_g.sum(axis=1).rename("starts")
    df = pd.concat([starts, per_g.idxmax(axis=1).rename("group")], axis=1).reset_index()
    df = df.merge(n_m, left_on="ts", right_index=True)
    df["share"] = df["starts"] / df["n_m"]
    if rule == "modal":
        key = df[df["share"] >= KEY_SHARE].copy()
        key["group_share"] = [per_g.loc[(r.ts, r.player_id), r.group] / r.n_m for r in key.itertuples()]
    elif rule == "strict":
        frac = per_g.div(n_m.reindex(per_g.index.get_level_values(0)).values, axis=0)
        ok = frac.max(axis=1) >= KEY_SHARE
        key = df.set_index(["ts", "player_id"]).loc[ok[ok].index].reset_index()
        key["group_share"] = frac.max(axis=1)[ok].values
    else:
        raise ValueError(rule)
    # importance tiers
    key["tier_share"] = np.where(key["share"] >= CORE_SHARE, "core", "regular")
    tot = a.groupby(["ts", "player_id"]).agg(minutes=("minutes", "sum"), xg=("xG", "sum"), xa=("xA", "sum"))
    tot["xgxa90"] = (tot["xg"] + tot["xa"]) / tot["minutes"].clip(lower=1) * 90
    elig = tot[tot["minutes"] >= ATT_MIN_MINUTES].copy()
    elig["att_rank"] = elig.groupby(level=0)["xgxa90"].rank(ascending=False, method="first")
    key = key.merge(tot[["minutes", "xgxa90"]], left_on=["ts", "player_id"], right_index=True, how="left")
    key = key.merge(elig[["att_rank"]], left_on=["ts", "player_id"], right_index=True, how="left")
    key["tier_att"] = np.where(key["att_rank"] <= ATT_TOP, "top3", "other")
    team_min = t.groupby("ts").size() * 90
    key["minutes_share"] = key["minutes"] / key["ts"].map(team_min)
    names = app.drop_duplicates(["player_id"]).set_index("player_id")["player"]
    key["player"] = key["player_id"].map(names)
    return key


def absences(tm, app, key):
    """Long table: one row per (team-match, key player) with absent flag + status."""
    starters = set(zip(app.loc[app.starter == 1, "match_id"], app.loc[app.starter == 1, "player_id"]))
    subs = app[app.starter == 0].set_index(["match_id", "player_id"])["minutes"].to_dict()
    x = tm[["match_id", "ts"]].merge(key, on="ts")
    pairs = list(zip(x["match_id"], x["player_id"]))
    x["absent"] = [int(p not in starters) for p in pairs]
    x["sub_min"] = [subs.get(p, 0) if p not in starters else 0 for p in pairs]
    x["abs_dnp"] = ((x["absent"] == 1) & (x["sub_min"] == 0)).astype(int)
    x["abs_sub"] = ((x["absent"] == 1) & (x["sub_min"] > 0)).astype(int)
    return x


def count_cols(tm, ab, by, flag="absent", prefix="n"):
    """Pivot absent key players into per-team-match counts; add own + opp columns."""
    ab = ab[ab[flag] == 1]
    if by is None:
        lab = pd.Series("ALL", index=ab.index)
    elif isinstance(by, str):
        lab = ab[by]
    else:
        lab = ab[by[0]] + "|" + ab[by[1]]
    c = ab.assign(lab=prefix + ":" + lab).pivot_table(index=["match_id", "ts"], columns="lab",
                                                       values=flag, aggfunc="sum", fill_value=0)
    cols = sorted(c.columns)
    own = tm[["match_id", "ts"]].merge(c, left_on=["match_id", "ts"], right_index=True, how="left")[cols].fillna(0)
    opp = tm[["match_id", "os"]].merge(c, left_on=["match_id", "os"], right_index=True, how="left")[cols].fillna(0)
    out = pd.concat([own.add_prefix("own|"), opp.add_prefix("opp|")], axis=1)
    out.index = tm.index
    return out


# ----------------------------------------------------------------------------- estimation
def fe_design(tm, extra=None):
    """[team-season dummies | opp-season dummies minus one per season | home | extra]"""
    ts_codes, ts_lev = pd.factorize(tm["ts"], sort=True)
    os_codes, os_lev = pd.factorize(tm["os"], sort=True)
    n = len(tm)
    A = np.zeros((n, len(ts_lev)))
    A[np.arange(n), ts_codes] = 1
    drop = set()
    seen = set()
    for j, lev in enumerate(os_lev):       # drop first opponent level of each season
        s = lev.split(":")[0]
        if s not in seen:
            seen.add(s)
            drop.add(j)
    keep = [j for j in range(len(os_lev)) if j not in drop]
    B = np.zeros((n, len(os_lev)))
    B[np.arange(n), os_codes] = 1
    parts = [A, B[:, keep], tm[["home"]].to_numpy(float)]
    names = [f"a|{l}" for l in ts_lev] + [f"d|{os_lev[j]}" for j in keep] + ["home"]
    if extra is not None:
        parts.append(extra.to_numpy(float))
        names += list(extra.columns)
    return np.hstack(parts), names


def cluster_meat(Xe, groups):
    codes, _ = pd.factorize(groups)
    S = np.zeros((codes.max() + 1, Xe.shape[1]))
    np.add.at(S, codes, Xe)
    G = codes.max() + 1
    return S.T @ S * G / (G - 1)


def ols(tm, Z, y="xg_for", extra_controls=None):
    """OLS with FE; returns dict of coefficients for Z columns with clustered SEs."""
    Z = Z.loc[:, (Z != 0).any()]
    ctrl = Z if extra_controls is None else pd.concat([extra_controls, Z], axis=1)
    X, names = fe_design(tm, ctrl)
    yv = tm[y].to_numpy(float)
    XtX_inv = np.linalg.inv(X.T @ X)
    b = XtX_inv @ (X.T @ yv)
    e = yv - X @ b
    Xe = X * e[:, None]
    V_t = XtX_inv @ cluster_meat(Xe, tm["ts"].to_numpy()) @ XtX_inv
    V_o = XtX_inv @ cluster_meat(Xe, tm["os"].to_numpy()) @ XtX_inv
    V_to = XtX_inv @ cluster_meat(Xe, (tm["ts"] + "|" + tm["os"]).to_numpy()) @ XtX_inv
    V_2 = V_t + V_o - V_to
    out = {}
    for c in Z.columns:
        j = names.index(c)
        side = c.split("|")[0]
        se_absent_team = np.sqrt(V_t[j, j] if side == "own" else V_o[j, j])
        out[c] = {"b": float(b[j]), "se": float(se_absent_team), "se_twoway": float(np.sqrt(max(V_2[j, j], 0)))}
    home_j = names.index("home")
    meta = {"n_rows": int(len(yv)), "home": float(b[home_j]), "resid_sd": float(e.std()),
            "n_team_seasons": int(tm["ts"].nunique())}
    return out, meta, b, names


def ppml(tm, Z, y="xg_for", offset=None, iters=50, tol=1e-10):
    """Poisson pseudo-ML with the same FE design (log link): exp(b)-1 = % effect."""
    X, names = fe_design(tm, Z)
    yv = tm[y].to_numpy(float)
    off = np.zeros(len(yv)) if offset is None else offset
    b = np.linalg.lstsq(X, np.log(np.maximum(yv, 0.05)) - off, rcond=None)[0]
    for _ in range(iters):
        eta = X @ b + off
        mu = np.exp(eta)
        z = eta - off + (yv - mu) / mu
        sw = np.sqrt(mu)
        b_new = np.linalg.lstsq(X * sw[:, None], z * sw, rcond=None)[0]
        if np.max(np.abs(b_new - b)) < tol:
            b = b_new
            break
        b = b_new
    mu = np.exp(X @ b + off)
    H_inv = np.linalg.inv((X * mu[:, None]).T @ X)
    Xe = X * (yv - mu)[:, None]
    V_t = H_inv @ cluster_meat(Xe, tm["ts"].to_numpy()) @ H_inv
    V_o = H_inv @ cluster_meat(Xe, tm["os"].to_numpy()) @ H_inv
    out = {}
    for c in Z.columns:
        j = names.index(c)
        se = np.sqrt(V_t[j, j] if c.startswith("own|") else V_o[j, j])
        out[c] = {"log_b": float(b[j]), "se": float(se), "pct": float(np.expm1(b[j]) * 100),
                  "pct_lo": float(np.expm1(b[j] - Z95 * se) * 100), "pct_hi": float(np.expm1(b[j] + Z95 * se) * 100)}
    return out, b, names


def fe_fit_predict(tr, te, ytr, log_link=False, offset_tr=None, offset_te=None):
    """Fit y ~ team-season + opp-season + home on tr rows, predict te rows (OLS or Poisson)."""
    both = pd.concat([tr, te])
    X, _ = fe_design(both)
    Xtr, Xte = X[:len(tr)], X[len(tr):]
    # drop columns that are all-zero in training (cannot happen with 10-fold, but be safe)
    if not log_link:
        b = np.linalg.lstsq(Xtr, ytr, rcond=None)[0]
        return Xte @ b
    otr = np.zeros(len(tr)) if offset_tr is None else offset_tr
    ote = np.zeros(len(te)) if offset_te is None else offset_te
    b = np.linalg.lstsq(Xtr, np.log(np.maximum(ytr, 0.05)) - otr, rcond=None)[0]
    for _ in range(50):
        eta = Xtr @ b + otr
        mu = np.exp(eta)
        z = eta - otr + (ytr - mu) / mu
        sw = np.sqrt(mu)
        bn = np.linalg.lstsq(Xtr * sw[:, None], z * sw, rcond=None)[0]
        if np.max(np.abs(bn - b)) < 1e-9:
            b = bn
            break
        b = bn
    return np.exp(Xte @ b + ote)


def mean_diff_ci(d, clusters):
    """Mean of d with SE clustered on `clusters`."""
    codes, _ = pd.factorize(clusters)
    G = codes.max() + 1
    m = d.mean()
    s = np.zeros(G)
    np.add.at(s, codes, d - m)
    se = np.sqrt((s ** 2).sum() * G / (G - 1)) / len(d)
    return float(m), float(se)


def calib_slope(r, s, clusters):
    """OLS of r on [1, s]; slope and its clustered SE (1 = train effects transfer fully)."""
    X = np.column_stack([np.ones_like(s), s])
    XtX_inv = np.linalg.inv(X.T @ X)
    b = XtX_inv @ X.T @ r
    e = r - X @ b
    V = XtX_inv @ cluster_meat(X * e[:, None], clusters) @ XtX_inv
    return float(b[1]), float(np.sqrt(V[1, 1]))


def oos_test(tm, Z, train_seasons, test_seasons, y="xg_for", multiplicative=False):
    """Fit absence coefficients on train seasons; cross-fit FE baseline on test seasons.

    Baseline A : y ~ team-season + opp-season + home                 (fit on other folds)
    Model    B : (y - Z b_train) ~ same FE; prediction = FE + Z b_train
    Also B_att (own terms only) and B_def (opponent terms only).
    """
    trm = tm["season"].isin(train_seasons).to_numpy()
    tem = tm["season"].isin(test_seasons).to_numpy()
    tr, te = tm[trm], tm[tem].copy()
    Zte = Z[tem]
    if multiplicative:
        coef, _, _ = ppml(tr, Z[trm], y=y)
        bvec = np.array([coef[c]["log_b"] if c in coef else 0.0 for c in Z.columns])
    else:
        coef, _, _, _ = ols(tr, Z[trm], y=y)
        bvec = np.array([coef[c]["b"] if c in coef else 0.0 for c in Z.columns])
    own = np.array([c.startswith("own|") for c in Z.columns])
    offs = {"B": Zte.to_numpy() @ bvec,
            "B_att": Zte.to_numpy()[:, own] @ bvec[own],
            "B_def": Zte.to_numpy()[:, ~own] @ bvec[~own]}
    rng = np.random.default_rng(SEED)
    mids = te["match_id"].unique()
    fold_of = dict(zip(mids, rng.permutation(len(mids)) % KFOLD))
    te["fold"] = te["match_id"].map(fold_of)
    yte = te[y].to_numpy(float)
    preds = {k: np.zeros(len(te)) for k in ["A", "B", "B_att", "B_def"]}
    for k in range(KFOLD):
        hold = (te["fold"] == k).to_numpy()
        a_tr, a_te = te[~hold], te[hold]
        preds["A"][hold] = fe_fit_predict(a_tr, a_te, yte[~hold], log_link=multiplicative)
        for name, off in offs.items():
            if multiplicative:
                preds[name][hold] = fe_fit_predict(a_tr, a_te, yte[~hold], True, off[~hold], off[hold])
            else:
                preds[name][hold] = fe_fit_predict(a_tr, a_te, yte[~hold] - off[~hold]) + off[hold]
    res = {"n_rows": int(len(te)), "n_matches": int(len(mids)), "train_seasons": train_seasons,
           "test_seasons": test_seasons, "mse_A": float(np.mean((yte - preds["A"]) ** 2)),
           "mae_A": float(np.mean(np.abs(yte - preds["A"])))}
    any_abs = (Zte.to_numpy() != 0).any(axis=1)
    for name in ["B", "B_att", "B_def"]:
        dA = (yte - preds["A"]) ** 2
        dB = (yte - preds[name]) ** 2
        diff = dA - dB  # >0 means absence terms reduce squared error
        m, se_ts = mean_diff_ci(diff, te["ts"].to_numpy())
        _, se_m = mean_diff_ci(diff, te["match_id"].to_numpy())
        se = max(se_ts, se_m)
        sub = any_abs if name == "B" else (Zte.to_numpy()[:, own] != 0).any(axis=1) if name == "B_att" \
            else (Zte.to_numpy()[:, ~own] != 0).any(axis=1)
        ms, ses = mean_diff_ci(diff[sub], te["ts"].to_numpy()[sub])
        slope, slope_se = calib_slope(yte - preds["A"], preds[name] - preds["A"], te["ts"].to_numpy())
        res[name] = {
            "calibration_slope": slope, "calibration_slope_se": slope_se,
            "mse": float(dB.mean()),
            "mse_reduction": m, "mse_reduction_se": se,
            "mse_reduction_ci95": [m - Z95 * se, m + Z95 * se],
            "mse_reduction_pct": float(100 * m / dA.mean()),
            "mae_reduction": float(np.mean(np.abs(yte - preds["A"]) - np.abs(yte - preds[name]))),
            "rows_with_absence": int(sub.sum()),
            "mse_reduction_rows_with_absence": ms,
            "mse_reduction_rows_with_absence_ci95": [ms - Z95 * ses, ms + Z95 * ses],
            "mse_reduction_rows_with_absence_pct": float(100 * ms / dA[sub].mean()),
        }
    return res


# ----------------------------------------------------------------------------- reporting helpers
def tidy(coef, ab, by, flag="absent", key=None):
    """coef dict -> list of rows {group, tier, d_xg_for, se, d_xg_against, se, n_...}."""
    rows = []
    labs = sorted({c.split("|", 1)[1] for c in coef})
    for lab in labs:
        rest = lab.split(":", 1)[1]
        parts = rest.split("|")
        g = parts[0]
        tier = parts[1] if len(parts) > 1 else "all"
        sel = ab[ab[flag] == 1]
        if by is not None:
            if isinstance(by, str):
                sel = sel[sel[by] == rest]
            else:
                sel = sel[(sel[by[0]] == parts[0]) & (sel[by[1]] == parts[1])]
        att = coef.get(f"own|{lab}")
        dfn = coef.get(f"opp|{lab}")
        r = {"group": g, "tier": tier,
             "d_xg_for": round(att["b"], 4), "se_for": round(att["se"], 4), "se_for_twoway": round(att["se_twoway"], 4),
             "d_xg_against": round(dfn["b"], 4), "se_against": round(dfn["se"], 4),
             "se_against_twoway": round(dfn["se_twoway"], 4),
             "n_absent_player_matches": int(len(sel)),
             "n_team_matches_with_absence": int(sel.drop_duplicates(["match_id", "ts"]).shape[0]),
             "n_key_player_seasons_absent": int(sel.drop_duplicates(["ts", "player_id"]).shape[0])}
        if key is not None and by is not None:
            k = key
            if isinstance(by, str):
                k = k[k[by] == rest]
            else:
                k = k[(k[by[0]] == parts[0]) & (k[by[1]] == parts[1])]
            r["n_key_player_seasons"] = int(len(k))
        rows.append(r)
    return rows


def fmt_table(rows, title):
    lines = [title, f"{'group':6s} {'tier':8s} {'dXGfor':>8s} {'se':>6s} {'dXGag':>8s} {'se':>6s} {'n_abs':>6s} {'n_ps':>5s}"]
    for r in rows:
        lines.append(f"{r['group']:6s} {r['tier']:8s} {r['d_xg_for']:+8.3f} {r['se_for']:6.3f} "
                     f"{r['d_xg_against']:+8.3f} {r['se_against']:6.3f} {r['n_absent_player_matches']:6d} "
                     f"{r.get('n_key_player_seasons', 0):5d}")
    return "\n".join(lines)


def main():
    t0 = time.time()
    tm, app = load()
    seasons = sorted(tm["season"].unique().tolist())
    print(f"rows={len(tm)} matches={tm.match_id.nunique()} seasons={seasons}")
    train = [s for s in TRAIN if s in seasons]
    test = [s for s in TEST if s in seasons]
    R = {"meta": {"seasons": seasons, "n_matches": int(tm.match_id.nunique()), "n_team_matches": int(len(tm)),
                  "train_seasons": train, "test_seasons": test,
                  "mean_xg_per_team_match": float(tm["xg_for"].mean()),
                  "mean_xg_home": float(tm.loc[tm.home == 1, "xg_for"].mean()),
                  "mean_xg_away": float(tm.loc[tm.home == 0, "xg_for"].mean())}}

    key = key_players(tm, app, "modal")
    ab = absences(tm, app, key)
    ab["line"] = ab["group"].map(LINE)
    key["line"] = key["group"].map(LINE)
    # absences before the player's first appearance for that team-season (not yet signed / not yet in squad)
    first_app = app.merge(tm[["match_id", "ts", "match_no"]], on=["match_id", "ts"]).groupby(["ts", "player_id"])["match_no"].min()
    abm = ab.merge(tm[["match_id", "ts", "match_no"]], on=["match_id", "ts"])
    abm["before_first"] = abm["match_no"] < abm.set_index(["ts", "player_id"]).index.map(first_app).to_numpy()
    R["key_summary_extra"] = {"absences_before_first_appearance_fraction":
                              float(abm.loc[abm.absent == 1, "before_first"].mean())}
    R["key_summary"] = {
        "n_key_player_seasons": int(len(key)),
        "per_team_season_mean": float(key.groupby("ts").size().mean()),
        "by_group": key.groupby("group").size().reindex(GROUPS).fillna(0).astype(int).to_dict(),
        "share_quantiles": key["share"].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(3).to_dict(),
        "absence_rate": float(ab["absent"].mean()),
        "absent_dnp_fraction": float(ab["abs_dnp"].sum() / max(ab["absent"].sum(), 1)),
        "group_share_of_starts_quantiles": key["group_share"].div(key["share"]).quantile([0.1, 0.25, 0.5]).round(3).to_dict(),
    }

    # --- main: pooled by position group
    Zg = count_cols(tm, ab, "group")
    cg, meta, _, _ = ols(tm, Zg)
    R["main_by_group"] = {"rows": tidy(cg, ab, "group", key=key), "fit": meta}
    print(fmt_table(R["main_by_group"]["rows"], "MAIN by group (modal key rule, all seasons)"))

    # --- tiers by start share
    Zs = count_cols(tm, ab, ["group", "tier_share"])
    cs, meta_s, _, _ = ols(tm, Zs)
    R["tier_start_share"] = {"rows": tidy(cs, ab, ["group", "tier_share"], key=key), "fit": meta_s,
                             "definition": f"core: started >= {CORE_SHARE:.0%} of team league matches; regular: {KEY_SHARE:.0%}-{CORE_SHARE:.0%}"}
    print(fmt_table(R["tier_start_share"]["rows"], "TIER by start share"))

    # --- tiers by attacking output rank
    Za = count_cols(tm, ab, ["group", "tier_att"])
    ca, meta_a, _, _ = ols(tm, Za)
    R["tier_attack_rank"] = {"rows": tidy(ca, ab, ["group", "tier_att"], key=key), "fit": meta_a,
                             "definition": f"top3: top-{ATT_TOP} in team-season (xG+xA)/90 among players with >= {ATT_MIN_MINUTES} league minutes"}
    print(fmt_table(R["tier_attack_rank"]["rows"], "TIER by attack rank"))

    # --- total count (one coefficient) + additivity check
    # --- coarse lines (GK / DEF / MID / ATT): fewer, more stable coefficients
    Zl = count_cols(tm, ab, "line")
    cl, meta_l, _, _ = ols(tm, Zl)
    R["by_line"] = {"rows": tidy(cl, ab, "line", key=key), "fit": meta_l, "definition": LINE}
    print(fmt_table(R["by_line"]["rows"], "BY LINE"))

    Zt = count_cols(tm, ab, None)
    ct, _, _, _ = ols(tm, Zt)
    nown, nopp = Zt["own|n:ALL"], Zt["opp|n:ALL"]
    Zq = Zg.copy()
    Zq["own|extra_pairs"] = np.maximum(nown - 1, 0)       # each absence beyond the first
    Zq["opp|extra_pairs"] = np.maximum(nopp - 1, 0)
    cq, _, _, _ = ols(tm, Zq)
    Zd = pd.DataFrame(index=tm.index)
    for k in (1, 2, 3):
        Zd[f"own|cnt{k}"] = (nown == k).astype(int)
        Zd[f"opp|cnt{k}"] = (nopp == k).astype(int)
    Zd["own|cnt4+"] = (nown >= 4).astype(int)
    Zd["opp|cnt4+"] = (nopp >= 4).astype(int)
    cd, _, _, _ = ols(tm, Zd)
    R["additivity"] = {
        "per_absence_all_groups": {"d_xg_for": ct["own|n:ALL"], "d_xg_against": ct["opp|n:ALL"]},
        "beyond_first_absence_term": {"for": cq["own|extra_pairs"], "against": cq["opp|extra_pairs"]},
        "count_dummies_vs_zero": {k: v for k, v in cd.items()},
        "distribution_own_count": nown.value_counts().sort_index().astype(int).to_dict(),
    }

    # --- PPML multiplicative version (pooled by group)
    cp, _, _ = ppml(tm, Zg)
    R["ppml_by_group"] = [{"group": c.split(":", 1)[1], "side": "xg_for" if c.startswith("own|") else "xg_against",
                           **{k: round(v, 4) for k, v in d.items()}} for c, d in cp.items()]

    # --- robustness
    rob = {}
    # (1) spec-literal strict key rule
    key_s = key_players(tm, app, "strict")
    ab_s = absences(tm, app, key_s)
    c1, _, _, _ = ols(tm, count_cols(tm, ab_s, "group"))
    rob["strict_key_rule"] = {"rows": tidy(c1, ab_s, "group", key=key_s), "n_key_player_seasons": int(len(key_s))}
    # (2) drop each team's last 5 matches (dead rubbers)
    keep = (tm["match_no"] <= tm["n_team_matches"] - 5).to_numpy()
    tmk = tm[keep].reset_index(drop=True)
    c2, _, _, _ = ols(tmk, Zg[keep].reset_index(drop=True))
    rob["drop_last5"] = {"rows": tidy(c2, ab[ab.match_id.isin(tmk.match_id)], "group")}
    # (3) rest-day controls (own + opp; league fixtures only)
    rc = pd.DataFrame(index=tm.index)
    rd = tm.set_index(["match_id", "ts"])["rest_days"]
    opp_rest = pd.Series([rd.get((m, o), 99.0) for m, o in zip(tm["match_id"], tm["os"])], index=tm.index)
    for lab, r in (("own", tm["rest_days"]), ("opp", opp_rest)):
        rc[f"ctl|{lab}_rest_le3"] = (r <= 3.5).astype(int)
        rc[f"ctl|{lab}_rest_4_5"] = ((r > 3.5) & (r <= 5.5)).astype(int)
    c3, _, _, _ = ols(tm, Zg, extra_controls=rc)
    rob["rest_day_controls"] = {"rows": tidy(c3, ab, "group")}
    # (4) did-not-play vs came-on-as-sub
    Zdn = count_cols(tm, ab, "group", flag="abs_dnp", prefix="dnp")
    Zsb = count_cols(tm, ab, "group", flag="abs_sub", prefix="sub")
    c4, _, _, _ = ols(tm, pd.concat([Zdn, Zsb], axis=1))
    rob["dnp_vs_benched"] = {"did_not_play": tidy({k: v for k, v in c4.items() if "dnp:" in k}, ab, "group", flag="abs_dnp"),
                             "came_on_as_sub": tidy({k: v for k, v in c4.items() if "sub:" in k}, ab, "group", flag="abs_sub")}
    # (5) stability: train-only vs test-only estimates
    for lab, ss in (("train_only", train), ("test_only", test)):
        if not ss:
            continue
        msk = tm["season"].isin(ss).to_numpy()
        c5, _, _, _ = ols(tm[msk].reset_index(drop=True), Zg[msk].reset_index(drop=True))
        rob[lab] = {"rows": tidy(c5, ab[ab.ts.isin(set(tm.loc[msk, "ts"]))], "group")}
    # (6) alternative mapping: FWL/FWR (wide forwards of a front three) -> WIDE instead of ST
    app_alt = app.copy()
    app_alt.loc[app_alt["position"].isin(["FWL", "FWR"]), "group"] = "WIDE"
    key_alt = key_players(tm, app_alt, "modal")
    ab_alt = absences(tm, app_alt, key_alt)
    c6, _, _, _ = ols(tm, count_cols(tm, ab_alt, "group"))
    rob["fwl_fwr_as_wide"] = {"rows": tidy(c6, ab_alt, "group", key=key_alt)}
    # (7) placebo: does NEXT match's absence count predict THIS match's xG (given this match's)?
    #     non-zero lead terms => absences follow form (selection), not only cause it
    own_cols = [c for c in Zl.columns if c.startswith("own|")]
    own = pd.concat([tm[["match_id", "ts", "match_no"]], Zl[own_cols]], axis=1)
    lead = own.sort_values(["ts", "match_no"]).groupby("ts")[own_cols].shift(-1)
    lead_tab = pd.concat([own[["match_id", "ts"]], lead], axis=1).set_index(["match_id", "ts"])
    Lown = lead_tab.reindex(list(zip(tm["match_id"], tm["ts"]))).reset_index(drop=True)
    Lopp = lead_tab.reindex(list(zip(tm["match_id"], tm["os"]))).reset_index(drop=True)
    Lown.columns = [c.replace("own|n:", "own|lead:") for c in own_cols]
    Lopp.columns = [c.replace("own|n:", "opp|lead:") for c in own_cols]
    okm = (Lown.notna().all(axis=1) & Lopp.notna().all(axis=1)).to_numpy()   # drop each team's last match
    Zp = pd.concat([Zl, Lown, Lopp], axis=1)[okm].reset_index(drop=True)
    c7, _, _, _ = ols(tm[okm].reset_index(drop=True), Zp)
    rob["placebo_lead_absence_by_line"] = {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in c7.items()}
    # (8) placebo split: was the player subbed off / injured in THIS match (minutes < 85) before
    #     missing the next one, or did he play the full match? Lead effects confined to the
    #     subbed-off case point to in-match injury; lead effects after a full 90 point to selection.
    abl = ab.merge(tm[["match_id", "ts", "match_no"]], on=["match_id", "ts"]).sort_values(["ts", "player_id", "match_no"])
    abl["next_absent"] = abl.groupby(["ts", "player_id"])["absent"].shift(-1)
    mins = app[app.starter == 1].set_index(["match_id", "player_id"])["minutes"]
    abl["min_now"] = mins.reindex(list(zip(abl["match_id"], abl["player_id"]))).to_numpy()
    ev = abl[(abl["absent"] == 0) & (abl["next_absent"] == 1)].copy()
    ev["kind"] = np.where(ev["min_now"] >= 85, "full", "off")
    ev["flag"] = 1
    ev["lab"] = ev["line"] + "|" + ev["kind"]
    cnt = ev.pivot_table(index=["match_id", "ts"], columns="lab", values="flag", aggfunc="sum", fill_value=0)
    cols = sorted(cnt.columns)
    Lo = tm[["match_id", "ts"]].merge(cnt, left_on=["match_id", "ts"], right_index=True, how="left")[cols].fillna(0)
    Lp = tm[["match_id", "os"]].merge(cnt, left_on=["match_id", "os"], right_index=True, how="left")[cols].fillna(0)
    Lo.columns = [f"own|lead:{c}" for c in cols]
    Lp.columns = [f"opp|lead:{c}" for c in cols]
    Lo.index = Lp.index = tm.index
    c8, _, _, _ = ols(tm[okm].reset_index(drop=True), pd.concat([Zl, Lo, Lp], axis=1)[okm].reset_index(drop=True))
    rob["placebo_lead_split_full90_vs_subbed_off"] = {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in c8.items()}
    rob["placebo_lead_split_event_counts"] = ev.groupby("lab").size().astype(int).to_dict()
    # (9) period split by line (is the effect stable across eras?)
    for lab, ss in (("by_line_train_only", train), ("by_line_test_only", test)):
        msk = tm["season"].isin(ss).to_numpy()
        c9, _, _, _ = ols(tm[msk].reset_index(drop=True), Zl[msk].reset_index(drop=True))
        c9t, _, _, _ = ols(tm[msk].reset_index(drop=True), Zt[msk].reset_index(drop=True))
        rob[lab] = {"rows": tidy(c9, ab[ab.ts.isin(set(tm.loc[msk, "ts"]))], "line"),
                    "any_key": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in c9t.items()}}
    # (10) recent-form controls: each side's mean xG-for / xG-against over its previous 5 league
    #      matches of the season (first match: league mean + a no-history flag)
    srt = tm.sort_values(["ts", "match_no"])
    form = pd.DataFrame(index=srt.index)
    for col in ("xg_for", "xg_against"):
        form[col] = srt.groupby("ts")[col].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    form = form.reindex(tm.index)
    nohist = form["xg_for"].isna().astype(int)
    form = form.fillna(tm["xg_for"].mean())
    ftab = pd.concat([tm[["match_id", "ts"]], form, nohist.rename("nohist")], axis=1).set_index(["match_id", "ts"])
    fopp = ftab.reindex(list(zip(tm["match_id"], tm["os"]))).reset_index(drop=True)
    fc = pd.DataFrame({"ctl|own_form_for": form["xg_for"].to_numpy(), "ctl|own_form_against": form["xg_against"].to_numpy(),
                       "ctl|own_nohist": nohist.to_numpy(), "ctl|opp_form_for": fopp["xg_for"].to_numpy(),
                       "ctl|opp_form_against": fopp["xg_against"].to_numpy()}, index=tm.index)
    c10, _, _, _ = ols(tm, Zg, extra_controls=fc)
    rob["recent_form_controls"] = {"rows": tidy(c10, ab, "group")}
    c10l, _, _, _ = ols(tm, Zl, extra_controls=fc)
    rob["recent_form_controls_by_line"] = {"rows": tidy(c10l, ab, "line")}
    R["robustness"] = rob

    # --- out-of-sample
    if train and test:
        R["oos"] = {
            "pooled_by_group_additive": oos_test(tm, Zg, train, test),
            "pooled_by_group_multiplicative": oos_test(tm, Zg, train, test, multiplicative=True),
            "tier_start_share_additive": oos_test(tm, Zs, train, test),
            "total_count_additive": oos_test(tm, Zt, train, test),
            "by_line_additive": oos_test(tm, Zl, train, test),
        }
        # no look-ahead key status: in test seasons, status comes from each team's first
        # 19 matches only and is evaluated on matches 20-38 (coefficients from train seasons)
        first_ts = tm[tm.match_no <= 19][["match_id", "ts"]]
        key_h = key_players(tm.merge(first_ts, on=["match_id", "ts"]),
                            app.merge(first_ts, on=["match_id", "ts"]), "modal")
        Zh = count_cols(tm, absences(tm, app, key_h), "group").reindex(columns=Zg.columns, fill_value=0)
        tr_m = tm.season.isin(train)
        te_m = tm.season.isin(test) & (tm.match_no > 19)
        tm2 = pd.concat([tm[tr_m], tm[te_m]]).reset_index(drop=True)
        Z2 = pd.concat([Zg[tr_m], Zh[te_m]]).reset_index(drop=True)
        R["oos"]["no_lookahead_status_second_half"] = oos_test(tm2, Z2, train, test)

    R["runtime_s"] = round(time.time() - t0, 1)
    json.dump(R, open(os.path.join(HERE, "results_full.json"), "w"), indent=1, default=float)
    print(json.dumps(R.get("oos", {}), indent=1, default=float)[:4000])
    print(f"done in {R['runtime_s']}s")


if __name__ == "__main__":
    main()
