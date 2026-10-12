"""The Odds API v4 client for systematic odds capture (closing snapshots).

Fills the biggest operational gap: closing prices no longer depend on the
user being awake to screenshot. Honest by construction:
- raw API responses land in odds/raw/ before any processing (doc-10 rule)
- no bookmaker is assumed: records Pinnacle when present, else lists what
  the response actually contains and records nothing without --book
- API failure -> report and exit; never fill values from memory

Setup: put the key in env ODDS_API_KEY or wc2026/config.json
       {"odds_api_key": "..."}  (config.json is gitignored)

Usage:
  python wc2026/fetch_odds.py sports                       # find the WC sport key
  python wc2026/fetch_odds.py list  --sport <key>          # events + quota
  python wc2026/fetch_odds.py grab  --sport <key> --event-id <id> \
         --match-id r16_can_mar [--closing] [--book pinnacle] [--regions eu]

`grab` REFUSES a non-closing snapshot when locks/<match-id>.json is absent
(PROTOCOL section 3: the lock must precede first odds contact, else the match
scores CONTAMINATED — the r16_por_esp / m92 failure). Lock first, then grab.
Override for a deliberate exception with --allow-unlocked (the snapshot is
marked contamination-exposed).

Cost = markets x regions per call. `grab` requests h2h+totals+spreads x 1 region
= 3 credits (1x2 is load-bearing and always recorded; totals/spreads are
best-effort and skipped with a printed note if the book doesn't carry them).
"to advance"/qualify is NOT offered by this provider for this competition
(confirmed live 2026-07-08) -- record that market by hand from a real
screenshot: `python wc2026/odds_snap.py <id> --book pinnacle --market qualify
--odds ... --outcomes home away --source "<screenshot provenance>"`.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
RAW_DIR = BASE / "odds" / "raw"
API = "https://api.the-odds-api.com/v4"


def api_key():
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        cfg = BASE / "config.json"
        if cfg.exists():
            key = json.loads(cfg.read_text(encoding="utf-8")).get("odds_api_key")
    if not key:
        sys.exit("no API key: set ODDS_API_KEY or wc2026/config.json "
                 '{"odds_api_key": "..."} — sign up at the-odds-api.com')
    return key


def get(path, **params):
    params["apiKey"] = api_key()
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "wc2026-harness"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
            quota = {k: r.headers.get(k) for k in
                     ("x-requests-remaining", "x-requests-used",
                      "x-requests-last")}
    except urllib.error.HTTPError as e:
        sys.exit(f"API error {e.code}: {e.read().decode('utf-8', 'replace')}")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = RAW_DIR / f"{stamp}_{path.strip('/').replace('/', '_')}.json"
    raw_path.write_text(body, encoding="utf-8")
    print(f"raw -> {raw_path}  quota: remaining={quota['x-requests-remaining']} "
          f"last-cost={quota['x-requests-last']}")
    return json.loads(body), raw_path


def cmd_sports(_args):
    data, _ = get("/sports/", all="true")
    for s in data:
        if "soccer" in s["key"] and ("world" in s["key"] or
                                     "fifa" in s["title"].lower() or
                                     "world cup" in s["title"].lower()):
            print(f"  {s['key']:<40} {s['title']}  active={s['active']}")


def cmd_list(args):
    data, _ = get(f"/sports/{args.sport}/odds/",
                  regions=args.regions, markets="h2h", oddsFormat="decimal")
    for ev in data:
        books = ",".join(b["key"] for b in ev.get("bookmakers", []))
        print(f"  {ev['id']}  {ev['home_team']} vs {ev['away_team']}  "
              f"ko={ev['commence_time']}  books=[{books}]")


def cmd_grab(args):
    sys.path.insert(0, str(BASE))
    from odds_snap import record

    # anchoring guard (PROTOCOL section 3), checked BEFORE spending an API credit:
    # the Phase-1 lock must PRECEDE first odds contact. A non-closing grab of a
    # match with no lock is exactly the r16_por_esp / m92 failure (odds landed
    # before any lock -> the match scores CONTAMINATED).
    lock_path = BASE / "locks" / f"{args.match_id}.json"
    unlocked = not lock_path.exists()
    if unlocked and not args.closing and not args.allow_unlocked:
        sys.exit(
            f"REFUSED: no lock at locks/{lock_path.name} — PROTOCOL section 3 "
            f"requires the Phase-1 lock to PRECEDE first odds contact, else the "
            f"match scores CONTAMINATED (as r16_por_esp and m92 did). Lock it "
            f"first:\n    python wc2026/lock.py create <payload.json>\n"
            f"then grab. For a deliberate exception (e.g. capturing a closing "
            f"price for an already-missing match), pass --allow-unlocked.")
    unlocked_note = ("UNLOCKED-GRAB (no prior lock; contamination-exposed); "
                     if unlocked and args.allow_unlocked else "")

    # h2h + totals + spreads in one call: confirmed live (2026-07-08) that
    # the-odds-api's soccer_fifa_world_cup odds endpoint serves all three from
    # Pinnacle for this competition. "to advance"/qualify is NOT available from
    # this provider at all (checked directly) -- that market still needs a
    # manual `odds_snap.py ... --market qualify` entry from a real screenshot;
    # don't build speculative code assuming an outrights-style key exists.
    data, raw_path = get(f"/sports/{args.sport}/odds/",
                         regions=args.regions, markets="h2h,totals,spreads",
                         oddsFormat="decimal")
    ev = next((e for e in data if e["id"] == args.event_id), None)
    if ev is None:
        sys.exit(f"event {args.event_id} not in response — run `list` first")
    closing = args.closing
    late_note = ""
    if closing and ev.get("commence_time"):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if now >= ev["commence_time"]:
            # a delayed task must never pass off an in-play price as the close
            closing = False
            late_note = " LATE GRAB after kickoff — closing flag stripped;"
            print(f"WARNING:{late_note} recording as non-closing snapshot")
    book = next((b for b in ev.get("bookmakers", [])
                 if b["key"] == args.book), None)
    if book is None:
        have = [b["key"] for b in ev.get("bookmakers", [])]
        sys.exit(f"bookmaker '{args.book}' not in response for this event; "
                 f"available: {have} — pass --book explicitly to use one")
    note = f"{late_note}{unlocked_note}{ev['home_team']} vs {ev['away_team']} ko={ev['commence_time']}"
    src = (f"the-odds-api {args.sport} event={args.event_id} raw={raw_path.name} "
           f"(book ts {{ts}})")
    recorded = []

    h2h = next((m for m in book["markets"] if m["key"] == "h2h"), None)
    if h2h is None:
        sys.exit(f"no h2h market from {args.book}")  # 1x2 is load-bearing; the others are best-effort
    by_name = {o["name"]: o["price"] for o in h2h["outcomes"]}
    try:
        odds = [by_name[ev["home_team"]], by_name["Draw"], by_name[ev["away_team"]]]
    except KeyError:
        sys.exit(f"unexpected outcome names: {list(by_name)} — not recording")
    entry = record(args.match_id, args.book, "1x2", odds, ["home", "draw", "away"],
                   source=src.format(ts=h2h.get("last_update", book.get("last_update"))),
                   closing=closing, note=note)
    recorded.append(f"1x2 {odds}")

    totals = next((m for m in book["markets"] if m["key"] == "totals"), None)
    if totals is None:
        print(f"NOTE: no totals market from {args.book} for this event — skipped")
    else:
        by_side = {o["name"]: o for o in totals["outcomes"]}
        try:
            line = by_side["Over"]["point"]
            t_odds = [by_side["Over"]["price"], by_side["Under"]["price"]]
        except KeyError:
            print(f"NOTE: unexpected totals outcome shape {totals['outcomes']} — skipped")
        else:
            record(args.match_id, args.book, "totals", t_odds, ["over", "under"],
                   source=src.format(ts=totals.get("last_update", book.get("last_update"))),
                   line=line, closing=closing, note=note)
            recorded.append(f"totals@{line} {t_odds}")

    spreads = next((m for m in book["markets"] if m["key"] == "spreads"), None)
    if spreads is None:
        print(f"NOTE: no spreads market from {args.book} for this event — skipped")
    else:
        by_side = {o["name"]: o for o in spreads["outcomes"]}
        try:
            home_o = by_side[ev["home_team"]]
            away_o = by_side[ev["away_team"]]
        except KeyError:
            print(f"NOTE: unexpected spreads outcome shape {spreads['outcomes']} — skipped")
        else:
            # odds_snap.py convention: --line is the HOME line (away is its negation)
            record(args.match_id, args.book, "ah", [home_o["price"], away_o["price"]],
                   ["home", "away"],
                   source=src.format(ts=spreads.get("last_update", book.get("last_update"))),
                   line=home_o["point"], closing=closing, note=note)
            recorded.append(f"ah@{home_o['point']} [{home_o['price']}, {away_o['price']}]")

    print(f"RECORDED {args.match_id} {args.book}: {'; '.join(recorded)} "
          f"closing={closing} @ {entry['fetched_at']}")


def main():
    ap = argparse.ArgumentParser(description="Odds API capture")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sports", help="list soccer/world-cup sport keys")
    l = sub.add_parser("list", help="list events with bookmakers")
    l.add_argument("--sport", required=True)
    l.add_argument("--regions", default="eu")
    g = sub.add_parser("grab", help="record one event's 1x2 snapshot")
    g.add_argument("--sport", required=True)
    g.add_argument("--event-id", required=True)
    g.add_argument("--match-id", required=True)
    g.add_argument("--book", default="pinnacle")
    g.add_argument("--regions", default="eu")
    g.add_argument("--closing", action="store_true")
    g.add_argument("--allow-unlocked", action="store_true",
                   help="override the no-lock refusal for a deliberate exception "
                        "(marks the snapshot contamination-exposed)")
    args = ap.parse_args()
    {"sports": cmd_sports, "list": cmd_list, "grab": cmd_grab}[args.cmd](args)


if __name__ == "__main__":
    main()
