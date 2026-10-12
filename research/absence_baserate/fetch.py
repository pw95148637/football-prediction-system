"""Polite, resumable Understat fetcher for EPL match rosters.

Endpoints (discovered from understat.com/js/league.min.js and match.min.js):
  GET https://understat.com/getLeagueData/EPL/<season>  -> {"teams","players","dates"}
  GET https://understat.com/getMatchData/<match_id>     -> {"rosters","shots","tmpl"}

Cache layout (relative to this file):
  cache/league/EPL_<season>.json   fixture list (forecast fields stripped)
  cache/match/<match_id>.json      {"rosters": ...} only (shots/tmpl dropped)

Rate limit: at most 1 request per second (MIN_INTERVAL); exponential backoff on
errors. Re-running skips everything already cached, so it can be interrupted.

Usage:  python fetch.py [season ...]      (default: all 2014..2025, interleaved)
"""
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
BASE = "https://understat.com"
MIN_INTERVAL = 1.1  # seconds between requests (<= 1 req/s)
MAX_TRIES = 7
# Interleave train/test seasons so a partial cache still supports the OOS split.
DEFAULT_SEASONS = [2014, 2022, 2015, 2023, 2016, 2024, 2017, 2025, 2018, 2019, 2020, 2021]

_last = [0.0]
S = requests.Session()
S.headers.update({
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (research; low-rate personal use)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
})


def get_json(path, referer):
    url = f"{BASE}/{path}"
    delay = 2.0
    for attempt in range(1, MAX_TRIES + 1):
        wait = MIN_INTERVAL - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = S.get(url, headers={"Referer": referer}, timeout=40)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                print(f"404 {url}", flush=True)
                return None
            err = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as e:
            err = repr(e)[:200]
        print(f"retry {attempt}/{MAX_TRIES} {url}: {err}; sleep {delay:.0f}s", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 120)
    raise RuntimeError(f"giving up on {url}")


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def league(season):
    os.makedirs(os.path.join(CACHE, "league"), exist_ok=True)
    p = os.path.join(CACHE, "league", f"EPL_{season}.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    d = get_json(f"getLeagueData/EPL/{season}", f"{BASE}/league/EPL/{season}")
    dates = []
    for m in d["dates"]:
        m = dict(m)
        m.pop("forecast", None)  # model win/draw/loss probabilities: not used, not kept
        dates.append(m)
    out = {"season": season, "dates": dates}
    atomic_write(p, out)
    return out


def match(mid):
    os.makedirs(os.path.join(CACHE, "match"), exist_ok=True)
    p = os.path.join(CACHE, "match", f"{mid}.json")
    if os.path.exists(p):
        return False
    d = get_json(f"getMatchData/{mid}", f"{BASE}/match/{mid}")
    if d is None:
        return False
    atomic_write(p, {"id": str(mid), "rosters": d["rosters"]})
    return True


def main(seasons):
    t0 = time.time()
    lg = {s: league(s) for s in seasons}
    for s in seasons:
        ids = [m["id"] for m in lg[s]["dates"] if m.get("isResult")]
        n_new = 0
        for i, mid in enumerate(ids):
            if match(mid):
                n_new += 1
            if n_new and n_new % 50 == 0:
                print(f"season {s}: {i + 1}/{len(ids)} ({time.time() - t0:.0f}s)", flush=True)
        print(f"DONE season {s}: {len(ids)} results, {n_new} newly fetched ({time.time() - t0:.0f}s)", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]] or DEFAULT_SEASONS
    main(args)
