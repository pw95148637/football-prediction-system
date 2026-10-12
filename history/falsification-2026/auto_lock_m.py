"""AUTO-M-LOCK: mechanical, price-blind M-only lock at pairing-known time.

Architecture Review v3, Migration Plan Phase 0(b) / PROTOCOL CHANGELOG
2026-07-08: the MISSING class of failure (m92 — no session showed up to lock
before kickoff) is structural, not a judgment error. M is pure arithmetic
(eloratings TSV -> calibrated mapping), so it can be locked the moment a
pairing is known, with no analyst in the loop and zero odds contact.

The J instrument is then added by an odds-clean analyst session via
`lock.py amend <match_id> <payload.json> --reason "instrument J synthesis
(pre-market, auto-M-lock process)"` BEFORE any odds contact for that match.
score.py audits this mechanically: the first lock version carrying "j" must
precede the match's first odds fetch, else the j is dropped from J scoring
(contaminated_j — por_esp/m97 precedent).

Honesty properties, by construction:
- raw TSVs land in data/elo_snapshots/ before any parsing (raw-first rule)
- team codes are cross-checked against en.teams.tsv; a mismatch aborts
- fetch failure aborts loudly; nothing is ever filled from memory
- the lock goes through lock.py create: immutable, hash-stamped, and the
  post-kickoff refusal guard applies (a late auto-lock records MISSING
  instead of pretending)
- this process touches eloratings.net ONLY — it cannot see an odds source

Usage (all fields explicit; the operator owns the match-id per SKILL rule):
  python wc2026/auto_lock_m.py --match-id m100_arg_sui --num 100 \
      --home Argentina --home-code AR --away Switzerland --away-code CH \
      --kickoff-utc 2026-07-11T20:00:00+00:00 \
      --venue "Arrowhead Stadium, Kansas City (neutral)" \
      --fixture-source "FIFA.com match page, accessed 2026-07-08" \
      [--host-bonus 0] [--pk 0.5] [--dry-run]

--host-bonus 75 applies ONLY when a host nation (USA/Mexico/Canada) plays
in its own country (pre-registered constant, PROTOCOL section 6); it flips
the calibrated mapping to neutral=False per model.predict_1x2's contract.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from lock import create_lock  # noqa: E402
from model import (predict_1x2, predict_lambdas,  # noqa: E402
                   qualify_prob_v2)

SNAP_DIR = BASE / "data" / "elo_snapshots"
WORLD_URL = "https://www.eloratings.net/World.tsv"
TEAMS_URL = "https://www.eloratings.net/en.teams.tsv"


def fetch_raw(url, stamp):
    """Fetch and land the raw file BEFORE parsing (raw-first rule).
    Any failure aborts — never fill ratings from memory."""
    req = urllib.request.Request(url, headers={"User-Agent": "wc2026-automlock"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
    except Exception as e:
        sys.exit(f"FETCH FAILED for {url}: {e} — aborting, no lock created "
                 f"(ratings are never filled from memory).")
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    raw_path = SNAP_DIR / f"{stamp}_{name}"
    raw_path.write_text(body, encoding="utf-8")
    return body, raw_path


def parse_ratings(world_tsv):
    """{code: rating} from World.tsv (no header; col2=code, col3=rating).
    eloratings.net uses U+2212 for minus in some columns; normalize."""
    ratings = {}
    for line in world_tsv.splitlines():
        cells = line.replace("−", "-").split("\t")
        if len(cells) < 4:
            continue
        code = cells[2].strip()
        try:
            ratings[code] = int(cells[3])
        except ValueError:
            continue
    if not ratings:
        sys.exit("PARSE FAILED: World.tsv yielded no (code, rating) rows — "
                 "format may have changed; aborting, no lock created.")
    return ratings


def parse_team_names(teams_tsv):
    """{code: name} from en.teams.tsv (code<TAB>name...)."""
    names = {}
    for line in teams_tsv.splitlines():
        cells = line.split("\t")
        if len(cells) >= 2 and cells[0].strip():
            names[cells[0].strip()] = cells[1].strip()
    if not names:
        sys.exit("PARSE FAILED: en.teams.tsv yielded no rows — aborting.")
    return names


def verify_code(code, expected_name, names):
    got = names.get(code)
    if got is None:
        sys.exit(f"CODE CHECK FAILED: '{code}' not in en.teams.tsv — "
                 f"aborting (wrong code would silently lock wrong ratings).")
    a, b = got.lower(), expected_name.lower()
    if a not in b and b not in a:
        sys.exit(f"CODE CHECK FAILED: en.teams.tsv maps '{code}' to '{got}', "
                 f"which does not match --home/--away '{expected_name}' — "
                 f"aborting. Pass the correct two-letter eloratings code.")
    return got


def build_payload(args, elo_home, elo_away, raw_paths, fetched_at):
    d_raw = elo_home - elo_away
    d_eff = d_raw + args.host_bonus
    neutral = args.host_bonus == 0
    w, d, l = predict_1x2(d_eff, neutral=neutral)
    lam_h, lam_a = predict_lambdas(d_eff)
    qualify = qualify_prob_v2(d_eff, pk_h=args.pk, neutral=neutral)
    return {
        "match_id": args.match_id,
        "pilot": False,
        "teams": {"home": args.home, "away": args.away},
        "kickoff_utc": args.kickoff_utc,
        "mode": "A",
        "probs_90": {"home": w, "draw": d, "away": l},
        "qualify_home": qualify,
        "lambdas": {"home": lam_h, "away": lam_a},
        "inputs": {
            "instrument": ("M v2 (calibration.json) — AUTO-M-LOCK: pure Elo, "
                           "no situational tuning; locked mechanically at "
                           "pairing-known time (CHANGELOG 2026-07-08)"),
            "elo_home": elo_home,
            "elo_away": elo_away,
            "elo_diff_raw": d_raw,
            "elo_diff_adjustment": args.host_bonus,
            "elo_diff_effective": d_eff,
            "adjustment_reasons": [
                ("+75 host-nation bonus (pre-registered, PROTOCOL section 6)"
                 if args.host_bonus else
                 "0 net, neutral venue: no host nation playing at home; "
                 "M kept as pure-Elo control — all situational synthesis "
                 "lives in J (added later via amend, pre-market)"),
            ],
            "pk_home": args.pk,
            "goals_note": "lambdas from calibrated curves",
        },
        "rationale": [
            (f"Elo {d_raw:+d}: {args.home} {elo_home} vs {args.away} "
             f"{elo_away}, eloratings.net World.tsv fetched {fetched_at} "
             f"(raw snapshots: {', '.join(p.name for p in raw_paths)})."),
            ("AUTO-M-LOCK: mechanical price-blind arithmetic at "
             "pairing-known time — eliminates the m92 MISSING failure "
             "class. No analyst judgment is embedded in this version."),
        ],
        "invalidation": [
            ("Situational facts (injuries/suspensions/lineups) belong to J "
             "or to a reasoned amend with the concrete new information — "
             "this v1 is the pure-Elo control and needs no invalidation "
             "conditions of its own."),
            ("Instrument J must be added by an odds-clean session via "
             "lock.py amend BEFORE any odds contact for this match; "
             "score.py drops a post-market j mechanically (contaminated_j)."),
        ],
        "sources": [
            {"item": (f"Elo {args.home} {elo_home} ({args.home_code}), "
                      f"{args.away} {elo_away} ({args.away_code}); "
                      f"diff {d_raw:+d}"),
             "source": f"{WORLD_URL} + {TEAMS_URL} (raw in data/elo_snapshots/)",
             "as_of": fetched_at},
            {"item": (f"Fixture: Match {args.num}, {args.venue}, "
                      f"ko {args.kickoff_utc}"),
             "source": args.fixture_source,
             "as_of": fetched_at[:10]},
        ],
        "contamination_notes": (
            "AUTO-M-LOCK (CHANGELOG 2026-07-08): this lock was produced "
            "mechanically from eloratings.net only — the process cannot see "
            "an odds source, and no analyst formed a judgment. Instrument J "
            "is absent in v1 by design; it must be added pre-market by an "
            "odds-clean session (score.py audits the j-bearing version's "
            "timestamp against first odds contact)."),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Mechanical price-blind M-only lock (auto-M-lock)")
    ap.add_argument("--match-id", required=True,
                    help="canonical id, e.g. m100_arg_sui (operator-owned, "
                         "must match all later odds/schedule registrations)")
    ap.add_argument("--num", required=True, type=int,
                    help="official match number (main sample: 89-104)")
    ap.add_argument("--home", required=True, help="nominal home team name")
    ap.add_argument("--away", required=True)
    ap.add_argument("--home-code", required=True,
                    help="two-letter eloratings.net code (verified against "
                         "en.teams.tsv)")
    ap.add_argument("--away-code", required=True)
    ap.add_argument("--kickoff-utc", required=True,
                    help="ISO 8601 UTC, e.g. 2026-07-11T20:00:00+00:00")
    ap.add_argument("--venue", required=True)
    ap.add_argument("--fixture-source", required=True,
                    help="provenance of the fixture facts (source + date)")
    ap.add_argument("--host-bonus", type=int, default=0, choices=[0, 75],
                    help="+75 ONLY for a host nation playing in its own "
                         "country (pre-registered constant)")
    ap.add_argument("--pk", type=float, default=0.5)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the payload, create no lock")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fetched_at = datetime.now(timezone.utc).isoformat()
    world, world_path = fetch_raw(WORLD_URL, stamp)
    teams, teams_path = fetch_raw(TEAMS_URL, stamp)
    ratings = parse_ratings(world)
    names = parse_team_names(teams)

    home_name = verify_code(args.home_code, args.home, names)
    away_name = verify_code(args.away_code, args.away, names)
    if args.home_code not in ratings or args.away_code not in ratings:
        sys.exit(f"RATING MISSING: {args.home_code} or {args.away_code} not "
                 f"in World.tsv — aborting.")
    elo_home, elo_away = ratings[args.home_code], ratings[args.away_code]
    print(f"Elo verified: {home_name} ({args.home_code}) {elo_home} | "
          f"{away_name} ({args.away_code}) {elo_away} | "
          f"raw -> {world_path.name}, {teams_path.name}")

    payload = build_payload(args, elo_home, elo_away,
                            [world_path, teams_path], fetched_at)
    p = payload["probs_90"]
    print(f"M v2: {args.home} {p['home']:.3f} / draw {p['draw']:.3f} / "
          f"{args.away} {p['away']:.3f}   qualify_home "
          f"{payload['qualify_home']:.3f}   lambdas "
          f"{payload['lambdas']['home']:.2f}/{payload['lambdas']['away']:.2f}")

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("DRY RUN — no lock created")
        return

    path = create_lock(payload)
    print(f"LOCKED {path}")
    print("NEXT: an odds-clean session adds instrument J pre-market via\n"
          f"  python wc2026/lock.py amend {args.match_id} <payload.json> "
          f"--reason \"instrument J synthesis (pre-market, auto-M-lock "
          f"process)\"\n"
          "and registers the closing-grab schedule under this exact "
          "match-id (SKILL rule).\n"
          "ALSO: re-run `python score.py` after this lock (and again after "
          "any J amend) — report.json otherwise goes stale and "
          "`football_ai.audit.selftest`'s bit-identical recompute check "
          "will FAIL on the next run (found 2026-07-13: new locks not "
          "reflected in report.json).")


if __name__ == "__main__":
    main()
