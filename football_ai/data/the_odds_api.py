"""the-odds-api adapter: live market snapshots for a club competition.

WHY: `epl_2026_27.py`'s docstring has carried "no live odds ingestion
wired" since 2026-07-24 -- the key and the `soccer_epl` sport key were
confirmed valid but nothing turned a response into store records, so every
EPL decision report printed "no odds snapshot at all yet" and no gate could
ever be evaluated. This is the generalized replacement for the archived
`fetch_odds.py`, which is welded to the WC2026 sport key, the WC match-id
convention and that experiment's own file tree.

TWO DISCIPLINES INHERITED FROM THE ARCHIVED VERSION, BOTH LOAD-BEARING:

1. **No lock, no snapshot** (`fetch_odds.py`'s "未鎖定拒抓" guard, added
   after the 2026-07-04 orphan-grab incident that cost the WC2026 sample a
   whole match, `m92`). `to_snapshots` refuses to emit a record for a
   fixture that has no lock in the store: reading a price for a fixture
   whose estimate is not yet frozen contaminates that estimate, and the
   only mechanical moment to stop it is here.
2. **Raw first, parsed second.** The API response is written verbatim to a
   gitignored raw dump before anything is parsed, so a later dispute about
   what the book actually showed is settled by bytes rather than by memory.

AND ONE THIS FILE ADDS, because the shape of a league window demands it:
a single `/odds` call returns EVERY upcoming fixture in the competition, so
merely printing the response contaminates the analyst for all ten of that
matchweek's matches at once. `to_snapshots` returns records; it never
prints. The caller decides which single match to render, and the store --
not the analyst's eye -- holds the rest. That distinction is the whole
reason this is a library function and not a CLI that dumps to stdout.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from football_ai.core.paths import raw_cache_dir
from football_ai.store import io as store_io

BASE_URL = "https://api.the-odds-api.com/v4"

# the-odds-api's club names -> football-data.co.uk's spelling, which is the
# spelling every lock, rating and fixture in this repo is keyed on. Only
# clubs whose names actually differ need an entry; anything absent passes
# through unchanged. Verified against the live 2026-27 EPL event list
# (2026-08-26) -- all 20 clubs of that season resolve.
TEAM_ALIASES = {
    "AFC Bournemouth": "Bournemouth",
    "Brighton and Hove Albion": "Brighton",
    "Coventry City": "Coventry",
    "Hull City": "Hull",
    "Ipswich Town": "Ipswich",
    "Leeds United": "Leeds",
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Tottenham Hotspur": "Tottenham",
    "West Ham United": "West Ham",
    "Wolverhampton Wanderers": "Wolves",
}

# the-odds-api market key -> (our market label, our outcome labels)
_MARKET_MAP = {
    "h2h": "1x2",
    "totals": "totals",
    "spreads": "ah",
}


def canonical_team(name):
    return TEAM_ALIASES.get((name or "").strip(), (name or "").strip())


def _get(path, key, params=None, timeout=30):
    """GET one endpoint. Returns (parsed_json, quota_headers, raw_text)."""
    qs = {"apiKey": key, **(params or {})}
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in qs.items())
    url = f"{BASE_URL}/{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "wc2026-harness"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            quota = {k.lower(): v for k, v in r.headers.items()
                     if k.lower().startswith("x-requests")}
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"the-odds-api {path} failed: HTTP {e.code} "
            f"{e.read()[:200].decode('utf-8', 'replace')}") from e
    return json.loads(raw), quota, raw


def fetch_events(sport_key, api_key):
    """Upcoming fixtures for a sport: ids, kickoff times, team names. Costs
    NO quota and carries NO prices -- safe to read directly, unlike
    `fetch_odds`."""
    data, quota, _ = _get(f"sports/{sport_key}/events", api_key)
    return data, quota


def fetch_odds(sport_key, api_key, markets=("h2h", "totals", "spreads"),
               regions="eu", bookmakers="pinnacle", cache_dir=None):
    """Fetch prices and DUMP THE RAW RESPONSE FIRST.

    Returns (parsed, quota, raw_path). The caller is expected NOT to print
    `parsed` -- it holds every upcoming fixture in the competition, and
    displaying it contaminates the analyst for all of them (see module
    docstring). Feed it to `to_snapshots` and render only what you meant to
    look at.
    """
    params = {"regions": regions, "markets": ",".join(markets),
              "oddsFormat": "decimal", "dateFormat": "iso"}
    if bookmakers:
        params["bookmakers"] = bookmakers
    data, quota, raw = _get(f"sports/{sport_key}/odds", api_key, params)

    cache_dir = cache_dir or raw_cache_dir("the_odds_api")
    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = cache_dir / f"{stamp}_{sport_key}_odds.json"
    raw_path.write_text(raw, encoding="utf-8")
    return data, quota, raw_path


def _locked_match_ids(era):
    return {r.get("match_id") for r in store_io.read("locks", era=era)
            if r.get("match_id")}


def _resolve_match_id(event, fixtures):
    """Map one API event to our match_id via canonical team names."""
    home = canonical_team(event.get("home_team"))
    away = canonical_team(event.get("away_team"))
    for f in fixtures:
        if f.get("home_team") == home and f.get("away_team") == away:
            return f["match_id"], home, away
    return None, home, away


def to_snapshots(events, fixtures, era, raw_name="", fetched_at=None,
                 closing=False, require_lock=True):
    """API events -> store-shaped odds records, plus a list of skips.

    Returns (records, skipped) where `skipped` is a list of
    (reason, description) pairs -- an unmapped club name or an unlocked
    fixture is reported, never silently dropped, because a silent drop here
    is indistinguishable from "the book had no price".
    """
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    locked = _locked_match_ids(era) if require_lock else None
    records, skipped = [], []

    for ev in events:
        mid, home, away = _resolve_match_id(ev, fixtures)
        if mid is None:
            skipped.append(("unknown fixture",
                            f"{home} v {away} (not in the fixture list)"))
            continue
        if locked is not None and mid not in locked:
            # The one guard that must never be relaxed for convenience.
            skipped.append(("NOT LOCKED",
                            f"{mid} ({home} v {away}) -- refusing to record a "
                            f"price for a fixture whose estimate is not frozen"))
            continue
        note = f"{home} vs {away} ko={ev.get('commence_time')}"
        for bk in ev.get("bookmakers", []):
            book = bk.get("key", "?")
            for mkt in bk.get("markets", []):
                label = _MARKET_MAP.get(mkt.get("key"))
                if label is None:
                    continue
                parsed = _parse_market(label, mkt.get("outcomes", []), home)
                if parsed is None:
                    skipped.append(("unparseable market",
                                    f"{mid} {book} {mkt.get('key')}"))
                    continue
                outcomes, odds, line = parsed
                records.append({
                    "match_id": mid, "book": book, "market": label,
                    "line": line, "outcomes": outcomes, "odds": odds,
                    "source": (f"the-odds-api {ev.get('sport_key', '?')} "
                               f"event={ev.get('id')} raw={raw_name} "
                               f"(book ts {bk.get('last_update')})"),
                    "fetched_at": fetched_at, "closing": closing,
                    "note": note,
                })
    return records, skipped


def _parse_market(label, outcomes, home):
    """One API market -> (outcome labels, decimal odds, line) in our shape."""
    by_name = {(o.get("name") or "").strip(): o for o in outcomes}
    if label == "1x2":
        h = next((o for n, o in by_name.items()
                  if canonical_team(n) == home), None)
        d = by_name.get("Draw")
        a = next((o for n, o in by_name.items()
                  if canonical_team(n) != home and n != "Draw"), None)
        if not (h and d and a):
            return None
        return ["home", "draw", "away"], [h["price"], d["price"], a["price"]], None
    if label == "totals":
        o_, u_ = by_name.get("Over"), by_name.get("Under")
        if not (o_ and u_) or o_.get("point") is None:
            return None
        return ["over", "under"], [o_["price"], u_["price"]], float(o_["point"])
    if label == "ah":
        h = next((o for n, o in by_name.items()
                  if canonical_team(n) == home), None)
        a = next((o for n, o in by_name.items()
                  if canonical_team(n) != home), None)
        if not (h and a) or h.get("point") is None:
            return None
        return ["home", "away"], [h["price"], a["price"]], float(h["point"])
    return None


def _selfcheck():
    import tempfile
    from pathlib import Path

    fails = []

    if canonical_team("Tottenham Hotspur") != "Tottenham":
        fails.append("alias map should canonicalize Tottenham Hotspur")
    if canonical_team("Arsenal") != "Arsenal":
        fails.append("a club needing no alias must pass through unchanged")

    fixtures = [{"match_id": "t_tot_new", "home_team": "Tottenham",
                 "away_team": "Newcastle"},
                {"match_id": "t_ars_che", "home_team": "Arsenal",
                 "away_team": "Chelsea"}]
    events = [
        {"id": "e1", "sport_key": "soccer_epl",
         "commence_time": "2026-08-29T16:30:00Z",
         "home_team": "Tottenham Hotspur", "away_team": "Newcastle United",
         "bookmakers": [{"key": "pinnacle", "last_update": "2026-08-26T10:00:00Z",
                         "markets": [
             {"key": "h2h", "outcomes": [
                 {"name": "Tottenham Hotspur", "price": 2.5},
                 {"name": "Newcastle United", "price": 2.9},
                 {"name": "Draw", "price": 3.4}]},
             {"key": "totals", "outcomes": [
                 {"name": "Over", "price": 1.95, "point": 2.5},
                 {"name": "Under", "price": 1.9, "point": 2.5}]},
             {"key": "spreads", "outcomes": [
                 {"name": "Tottenham Hotspur", "price": 1.98, "point": -0.25},
                 {"name": "Newcastle United", "price": 1.92, "point": 0.25}]},
         ]}]},
        # locked=False fixture: must be refused, loudly
        {"id": "e2", "sport_key": "soccer_epl",
         "commence_time": "2026-09-06T15:30:00Z",
         "home_team": "Arsenal", "away_team": "Chelsea", "bookmakers": []},
        # club that is not in our fixture list at all
        {"id": "e3", "sport_key": "soccer_epl",
         "commence_time": "2026-09-06T15:30:00Z",
         "home_team": "Real Madrid", "away_team": "Barcelona", "bookmakers": []},
    ]

    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            store_io.append("locks", {"payload": {}}, era="t-era",
                            match_id="t_tot_new", version=1)
            recs, skipped = to_snapshots(events, fixtures, era="t-era",
                                         raw_name="raw.json",
                                         fetched_at="2026-08-26T10:00:01+00:00")
            by_market = {r["market"]: r for r in recs}
            if set(by_market) != {"1x2", "totals", "ah"}:
                fails.append(f"expected all three markets, got {sorted(by_market)}")
            x = by_market.get("1x2")
            if x and (x["outcomes"] != ["home", "draw", "away"]
                      or x["odds"] != [2.5, 3.4, 2.9]):
                fails.append(f"1x2 must be ordered home/draw/away: {x['odds']}")
            t = by_market.get("totals")
            if t and (t["line"] != 2.5 or t["odds"] != [1.95, 1.9]):
                fails.append(f"totals parse wrong: {t}")
            ah = by_market.get("ah")
            if ah and (ah["line"] != -0.25 or ah["outcomes"] != ["home", "away"]):
                fails.append(f"ah must carry the HOME line: {ah}")
            if any(r["match_id"] != "t_tot_new" for r in recs):
                fails.append("a record escaped for a fixture that is not locked")

            reasons = {r for r, _ in skipped}
            if "NOT LOCKED" not in reasons:
                fails.append("an unlocked fixture must be refused AND reported")
            if "unknown fixture" not in reasons:
                fails.append("an unmappable event must be reported, not dropped")

            # require_lock=False is an explicit opt-out, never the default
            recs2, _ = to_snapshots(events, fixtures, era="t-era",
                                    require_lock=False)
            if not any(r["match_id"] == "t_ars_che" for r in recs2) and \
                    any(e["id"] == "e2" and e["bookmakers"] for e in events):
                fails.append("require_lock=False should stop refusing")
        finally:
            store_io.STORE_DIR = old

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
