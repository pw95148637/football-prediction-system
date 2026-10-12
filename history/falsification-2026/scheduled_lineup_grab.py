"""One-shot lineup-window (T-80min) odds snapshot, fired by Task Scheduler.

Sibling of scheduled_closing_grab.py WITHOUT the --closing flag: captures
the price state around lineup-announcement time so lineup-driven moves are
in the record (the m91 lesson's logging half — decisions still need a
session; this only leaves a trace). The fetch_odds no-lock guard applies:
an unlocked match is refused, exactly as it should be.

Usage: python scheduled_lineup_grab.py <match_id> <home_team> <away_team>
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_odds import get


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: scheduled_lineup_grab.py <match_id> <home> <away>")
    match_id, home, away = sys.argv[1], sys.argv[2], sys.argv[3]
    data, _ = get("/sports/soccer_fifa_world_cup/odds/", regions="eu,uk,us",
                  markets="h2h", oddsFormat="decimal")
    ev = next((e for e in data if e["home_team"] == home
               and e["away_team"] == away), None)
    if ev is None:
        available = [(e["home_team"], e["away_team"]) for e in data]
        sys.exit(f"FAILED: no event for {home} vs {away}. "
                 f"Available fixtures right now: {available}")
    print(f"found event {ev['id']} for {home} vs {away}, "
          f"ko={ev['commence_time']}")
    subprocess.run([sys.executable,
                    str(Path(__file__).parent / "fetch_odds.py"), "grab",
                    "--sport", "soccer_fifa_world_cup",
                    "--event-id", ev["id"], "--match-id", match_id,
                    "--book", "pinnacle", "--regions", "eu,uk,us"],
                   check=True)


if __name__ == "__main__":
    main()
