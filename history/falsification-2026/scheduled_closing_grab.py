"""One-shot closing-snapshot grab, meant to be fired by a local OS scheduled
task (Windows Task Scheduler) a few minutes before kickoff -- NOT a cloud
routine, since fetch_odds.py needs the local config.json API key and writes
to local odds/*.jsonl (a cloud CCR sandbox has no access to either).

Looks the event up by team names at run time instead of a baked-in event id,
since the-odds-api id could in principle change between scheduling and firing.

Usage: python scheduled_closing_grab.py <match_id> <home_team> <away_team>
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_odds import get


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: scheduled_closing_grab.py <match_id> <home_team> <away_team>")
    match_id, home, away = sys.argv[1], sys.argv[2], sys.argv[3]
    data, _ = get("/sports/soccer_fifa_world_cup/odds/", regions="eu,uk,us",
                  markets="h2h", oddsFormat="decimal")
    ev = next((e for e in data if e["home_team"] == home and e["away_team"] == away), None)
    if ev is None:
        available = [(e["home_team"], e["away_team"]) for e in data]
        sys.exit(f"FAILED: no event found for {home} vs {away}. "
                 f"Available fixtures right now: {available}")
    print(f"found event {ev['id']} for {home} vs {away}, ko={ev['commence_time']}")
    subprocess.run([sys.executable, str(Path(__file__).parent / "fetch_odds.py"), "grab",
                    "--sport", "soccer_fifa_world_cup", "--event-id", ev["id"],
                    "--match-id", match_id, "--book", "pinnacle",
                    "--regions", "eu,uk,us", "--closing"], check=True)


if __name__ == "__main__":
    main()
