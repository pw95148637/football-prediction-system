"""Turn the Understat cache into tidy per-match / per-appearance files.

Outputs (data/):
  matches.jsonl     one line per match: id, season, date, home/away team, xG,
                    goals, both starting XIs (Understat position + group) and
                    substitutes used with minutes
  appearances.csv   one row per player appearance (starter or used sub)
  build_log.json    counts and anomalies

Usage: python build.py
"""
import csv
import html
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "data")
SEASONS = list(range(2014, 2026))  # Understat season key = starting year

# Understat formation slot -> position group (MR/ML become FB under the back-three rule
# in side_lineup)
POS_GROUP = {
    "GK": "GK",
    "DC": "CB",
    "DR": "FB", "DL": "FB", "DMR": "FB", "DML": "FB",
    "DMC": "DM",
    "MC": "CM",
    "MR": "WIDE", "ML": "WIDE", "AMR": "WIDE", "AML": "WIDE",
    "AMC": "AM",
    "FW": "ST", "FWL": "ST", "FWR": "ST",
}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def side_lineup(roster):
    starters, subs = [], []
    for r in roster.values():
        rec = {
            "player_id": int(r["player_id"]),
            "player": html.unescape(r["player"]),
            "minutes": int(r["time"]),
            "xG": round(f(r["xG"]), 4),
            "xA": round(f(r["xA"]), 4),
        }
        if r["position"] == "Sub":
            subs.append(rec)
        else:
            rec["position"] = r["position"]
            rec["group"] = POS_GROUP.get(r["position"], "UNK")
            rec["pos_order"] = int(r.get("positionOrder") or 0)
            starters.append(rec)
    starters.sort(key=lambda x: x["pos_order"])
    # Back-three context rule: with exactly 3 DC and no DR/DL/DMR/DML in the XI, the
    # MR/ML slots are wing-backs (Understat/Opta label 3-4-3 wing-backs as MR/ML).
    slots = [p["position"] for p in starters]
    if slots.count("DC") == 3 and not any(s in ("DR", "DL", "DMR", "DML") for s in slots):
        for p in starters:
            if p["position"] in ("MR", "ML"):
                p["group"] = "FB"
                p["wingback_rule"] = True
    return starters, subs


def main():
    os.makedirs(OUT, exist_ok=True)
    log = Counter()
    unknown_pos = Counter()
    bad = []
    n_written = 0
    with open(os.path.join(OUT, "matches.jsonl"), "w", encoding="utf-8") as fm, \
            open(os.path.join(OUT, "appearances.csv"), "w", newline="", encoding="utf-8") as fa:
        w = csv.writer(fa)
        w.writerow(["match_id", "season", "date", "team", "opp", "h_a", "player_id", "player",
                    "starter", "position", "group", "minutes", "xG", "xA"])
        for season in SEASONS:
            lp = os.path.join(CACHE, "league", f"EPL_{season}.json")
            if not os.path.exists(lp):
                continue
            for m in json.load(open(lp, encoding="utf-8"))["dates"]:
                if not m.get("isResult"):
                    continue
                log["fixtures_with_result"] += 1
                mp = os.path.join(CACHE, "match", f"{m['id']}.json")
                if not os.path.exists(mp):
                    log["missing_roster"] += 1
                    continue
                ros = json.load(open(mp, encoding="utf-8"))["rosters"]
                rec = {
                    "id": int(m["id"]), "season": season, "date": m["datetime"][:10],
                    "datetime": m["datetime"],
                    "home": m["h"]["title"], "away": m["a"]["title"],
                    "xg_home": f(m["xG"]["h"]), "xg_away": f(m["xG"]["a"]),
                    "goals_home": int(m["goals"]["h"]), "goals_away": int(m["goals"]["a"]),
                }
                ok = True
                for side, team, opp in (("h", rec["home"], rec["away"]), ("a", rec["away"], rec["home"])):
                    st, sb = side_lineup(ros.get(side, {}))
                    if len(st) != 11:
                        ok = False
                        bad.append({"id": rec["id"], "side": side, "n_starters": len(st)})
                    for p in st:
                        if p["group"] == "UNK":
                            unknown_pos[p["position"]] += 1
                    rec[f"xi_{side}"] = st
                    rec[f"subs_{side}"] = sb
                    for p in st:
                        w.writerow([rec["id"], season, rec["date"], team, opp, side, p["player_id"], p["player"],
                                    1, p["position"], p["group"], p["minutes"], p["xG"], p["xA"]])
                    for p in sb:
                        w.writerow([rec["id"], season, rec["date"], team, opp, side, p["player_id"], p["player"],
                                    0, "Sub", "", p["minutes"], p["xG"], p["xA"]])
                rec["lineups_ok"] = ok
                log["lineups_ok" if ok else "lineups_bad"] += 1
                fm.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_written += 1
    out = {"matches_written": n_written, **log, "unknown_positions": dict(unknown_pos),
           "bad_lineups": bad[:50], "n_bad_lineups": len(bad)}
    json.dump(out, open(os.path.join(OUT, "build_log.json"), "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "bad_lineups"}, indent=1))


if __name__ == "__main__":
    main()
