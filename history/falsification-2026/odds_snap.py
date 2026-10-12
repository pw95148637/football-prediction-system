"""Append-only odds snapshots, one JSONL file per match.

Raw prices only — de-vig happens at scoring time (PROTOCOL.md: raw data must
land on disk; never store only processed results). Every snapshot must come
from an actual fetch at record time — never from memory, never estimated.

Conventions:
- market "1x2": outcomes home draw away (90 minutes).
- market "ah": outcomes home away; --line is the HOME line (away = -line).
- market "totals": outcomes over under; --line is the goals line.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ODDS_DIR = Path(__file__).parent / "odds"


def record(match_id, book, market, odds, outcomes, source,
           line=None, closing=False, note=None, odds_dir=ODDS_DIR):
    if len(odds) != len(outcomes):
        raise ValueError("odds and outcomes must have the same length")
    if any(o <= 1.0 for o in odds):
        raise ValueError("decimal odds must be > 1.0")
    odds_dir.mkdir(parents=True, exist_ok=True)
    path_existing = odds_dir / f"{match_id}.jsonl"
    if path_existing.exists():
        # dedupe: identical odds for the same book/market/line within 30 min
        # (script reruns must not double-record)
        now = datetime.now(timezone.utc)
        for line_txt in reversed(
                path_existing.read_text(encoding="utf-8").splitlines()):
            if not line_txt.strip():
                continue
            prev = json.loads(line_txt)
            if (prev["book"], prev["market"], prev.get("line")) != \
                    (book, market, line):
                continue
            same_odds = prev["odds"] == list(odds)
            age = (now - datetime.fromisoformat(
                prev["fetched_at"])).total_seconds()
            if same_odds and age < 1800:
                print(f"DEDUP: identical {book}/{market}/{line} snapshot "
                      f"{age:.0f}s old — not re-recording")
                return prev
            break  # only compare against the latest same-key entry
    entry = {
        "match_id": match_id,
        "book": book,
        "market": market,
        "line": line,
        "outcomes": outcomes,
        "odds": odds,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "closing": bool(closing),
        "note": note,
    }
    path = odds_dir / f"{match_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def main(argv=None):
    ap = argparse.ArgumentParser(description="Record a raw odds snapshot")
    ap.add_argument("match_id")
    ap.add_argument("--book", required=True)
    ap.add_argument("--market", required=True, choices=["1x2", "ah", "totals", "qualify"])
    ap.add_argument("--odds", required=True, nargs="+", type=float)
    ap.add_argument("--outcomes", required=True, nargs="+")
    ap.add_argument("--line", type=float, default=None)
    ap.add_argument("--source", required=True, help="URL or exact provenance")
    ap.add_argument("--closing", action="store_true")
    ap.add_argument("--note", default=None)
    args = ap.parse_args(argv)
    entry = record(args.match_id, args.book, args.market, args.odds,
                   args.outcomes, args.source, line=args.line,
                   closing=args.closing, note=args.note)
    print(f"RECORDED {args.match_id} {args.book} {args.market} "
          f"@ {entry['fetched_at']}")


if __name__ == "__main__":
    main()
