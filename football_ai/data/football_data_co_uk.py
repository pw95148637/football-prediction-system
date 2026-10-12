"""Results (+ historical closing odds) adapter for football-data.co.uk.

WHY: the multi-league generalization effort (git f01aed1; audit:
`research/audit/final_report.md`) needs a real results source
for a domestic league, structurally different from the archived experiment's
martj42/international_results feed (auto_lock_m.py, calibrate.py). This
module fetches and normalizes football-data.co.uk's free per-season CSVs
into the exact shape `models/m_dc.py`'s `replay()` already consumes --
records with `date`, `home_team`, `away_team`, `home_score`, `away_score`,
`neutral`, `tournament` -- so the existing Elo-replay/K-factor/goal-model
machinery (confirmed generic by the audit) needs zero changes to run on a
new league, only a different input frame.

football-data.co.uk uses the SAME column schema across all its European
divisions (E0=English Premier League, D1=German Bundesliga, SP1=Spanish La
Liga, I1=Italian Serie A, F1=French Ligue 1, ...), so this adapter is not
EPL-specific -- any of those divisions works by passing a different
`division` code. That is the actual "swap the data source" deliverable; it
also makes the other four divisions listed above close to free to add later.

A domestic league match is never neutral-venue (unlike WC2026's knockout
ties) -- every parsed record sets `neutral=False`, matching
`HomeAdvantageModel(mode="standard")`'s expectation (competition.py).
`tournament` is set to a constant per division (there is no competition-tier
mixing within one division's file, unlike the international feed that mixes
friendlies/qualifiers/World Cups) -- a single-competition league can lean on
`EloTierTable.default_k` alone, exactly as competition.py's docstring
describes.
"""
import csv
import io
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from football_ai.core.paths import raw_cache_dir

BASE_URL = "https://www.football-data.co.uk/mmz4281"

# Division code -> (title, used as the constant `tournament` label).
# football-data.co.uk's own naming (data.php); not exhaustive, add more as
# needed -- the CSV schema is identical across all of them.
DIVISIONS = {
    "E0": "English Premier League",
    "E1": "English Championship",
    "SP1": "Spanish La Liga",
    "I1": "Italian Serie A",
    "D1": "German Bundesliga",
    "F1": "French Ligue 1",
}

# A PRICE-FREE copy of the results, committed to git. The raw season files are
# gitignored (bulky, re-fetchable) and carry bookmaker odds columns, so a fresh
# checkout -- a cloud session whose network allowlist does not include
# football-data.co.uk, say -- has no way to compute ratings. This directory
# holds the same rows reduced to the six columns the project actually needs
# (division, date, teams, full-time score) and only played matches, refreshed
# by the unattended job. It never contains a price.
SNAPSHOT_DIR = Path(__file__).resolve().parent / "results_snapshot"
_SNAPSHOT_COLS = ("Div", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG")

# Closing-price columns football-data.co.uk uses for Pinnacle 1X2 odds.
# Present from roughly the 2019-20 season onward; older files omit them.
_PINNACLE_CLOSE_COLS = ("PSCH", "PSCD", "PSCA")


def season_code(start_year):
    """2025 -> "2526" (football-data.co.uk's season-folder naming)."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


class SeasonNotPublished(LookupError):
    """This division/season CSV does not exist upstream yet. Distinct from
    a transport failure: football-data.co.uk publishes a season's file only
    once that season has started, so a caller asking for the season now in
    progress can legitimately get a 404 in the pre-season weeks and should
    be able to carry on with the completed seasons (see `load_seasons`'s
    `missing_ok`) rather than die."""


def current_season_start_year(today=None):
    """Start year of the season in progress. European league seasons run
    August->May, so July onward already belongs to the season starting that
    calendar year (2026-08-31 -> 2026; 2027-03-01 -> 2026)."""
    today = today or datetime.now(timezone.utc).date()
    return today.year if today.month >= 7 else today.year - 1


_FALLBACK_SEEN = {}


def _warn_fallback(kind, message):
    """Say once per process that a fallback source is in use, then count the
    rest. A rating replay reads a dozen seasons, and a dozen identical
    WARNING lines on every command look like twelve separate failures when
    they are one expected condition (the cloud allowlist blocks the site)."""
    n = _FALLBACK_SEEN.get(kind, 0)
    _FALLBACK_SEEN[kind] = n + 1
    if n == 0:
        print(message + " [further seasons use the same source silently]",
              file=sys.stderr)


def snapshot_path(division, start_year, snapshot_dir=None):
    return Path(snapshot_dir or SNAPSHOT_DIR) / f"{division}_{season_code(start_year)}.csv"


def write_snapshot(text, division, start_year, snapshot_dir=None):
    """Write the price-free projection of a raw season CSV; True if the file
    changed. Only played matches and only `_SNAPSHOT_COLS` survive, so no odds
    column and no unplayed fixture can reach the repository."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(_SNAPSHOT_COLS)
    for row in csv.DictReader(io.StringIO(text)):
        if not row.get("Date") or not row.get("HomeTeam"):
            continue
        if row.get("FTHG") in (None, "") or row.get("FTAG") in (None, ""):
            continue
        w.writerow([(row.get(c) or "").strip() for c in _SNAPSHOT_COLS])
    path = snapshot_path(division, start_year, snapshot_dir)
    new = out.getvalue()
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def refresh_snapshots(division="E0", from_year=2015, snapshot_dir=None, cache_dir=None):
    """Bring the committed snapshot up to date. A completed season never
    changes, so it is fetched once; the season in progress is refetched every
    time. Returns the list of snapshot files that changed."""
    changed = []
    cur = current_season_start_year()
    for y in range(from_year, cur + 1):
        if y < cur and snapshot_path(division, y, snapshot_dir).exists():
            continue
        try:
            text = _fetch_raw(division, y, cache_dir=cache_dir, force=(y == cur),
                              snapshot_dir=snapshot_dir, use_snapshot=False)
        except SeasonNotPublished:
            continue
        if write_snapshot(text, division, y, snapshot_dir):
            changed.append(snapshot_path(division, y, snapshot_dir).name)
    return changed


def _fetch_raw(division, start_year, cache_dir=None, force=False,
               max_age_hours=None, snapshot_dir=None, use_snapshot=True):
    """Return the raw CSV text for one division/season, fetching over HTTP
    only if not already cached (mirrors fetch_odds.py's raw-dump discipline:
    write what was actually received, don't silently refetch).

    `max_age_hours` EXPIRES the cache, and the season in progress needs it.
    A COMPLETED season's file never changes, so caching it forever is right;
    the CURRENT season's file grows every matchweek, and caching that
    forever is a bug with a long fuse -- the settle job would keep reading
    the copy taken before the fixtures were played and report "the source
    has no result yet" for the rest of the season, which is
    indistinguishable from the source genuinely lagging. Caught 2026-08-31
    before it bit: the cached copy happened to still match the live one, so
    nothing had gone wrong yet.
    """
    cache_dir = cache_dir or raw_cache_dir("football_data_co_uk")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{division}_{season_code(start_year)}.csv"
    if path.exists() and not force and max_age_hours is not None:
        age_h = (datetime.now(timezone.utc).timestamp()
                 - path.stat().st_mtime) / 3600.0
        if age_h > max_age_hours:
            force = True
    if path.exists() and not force:
        return path.read_text(encoding="utf-8-sig")
    url = f"{BASE_URL}/{season_code(start_year)}/{division}.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "wc2026-harness"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8-sig")
    except (urllib.error.URLError, OSError) as e:
        # A 404 means "not published yet" (handled below). Anything else --
        # 5xx, timeout, DNS -- is the SOURCE being unwell, not an answer. With
        # a cached copy, serve it and say so: the hourly settle job used to
        # die with a traceback every hour for five days (2026-09-05..10, HTTP
        # 503) although the copy on disk was at most a few hours stale.
        # Without a cache there is nothing to fall back on, so the error
        # stands. Raise, never sys.exit: this is a library function, and
        # killing the interpreter from inside a data adapter takes the
        # caller's whole session down.
        not_published = isinstance(e, urllib.error.HTTPError) and e.code == 404
        if not not_published and path.exists():
            when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            _warn_fallback("cached copy",
                           f"WARNING: football-data.co.uk unreachable for "
                           f"{division} {start_year}-{start_year + 1} ({e}); "
                           f"using the cached copy from "
                           f"{when.isoformat(timespec='minutes')}")
            return path.read_text(encoding="utf-8-sig")
        snap = snapshot_path(division, start_year, snapshot_dir)
        if not not_published and use_snapshot and snap.exists():
            when = datetime.fromtimestamp(snap.stat().st_mtime, timezone.utc)
            _warn_fallback("git snapshot",
                           f"WARNING: football-data.co.uk unreachable for "
                           f"{division} {start_year}-{start_year + 1} ({e}) "
                           f"and no cached copy; using the price-free "
                           f"snapshot in git (file dated "
                           f"{when.isoformat(timespec='minutes')}; the "
                           f"unattended job refreshes it)")
            return snap.read_text(encoding="utf-8-sig")
        if not_published:
            raise SeasonNotPublished(
                f"football-data.co.uk has no {division} file for "
                f"{start_year}-{start_year + 1} yet ({url})") from e
        code = getattr(e, "code", None)
        raise RuntimeError(
            f"football-data.co.uk fetch failed for {division} "
            f"{start_year}-{start_year + 1}: "
            f"{'HTTP ' + str(code) if code else e}") from e
    path.write_text(text, encoding="utf-8")
    return text


def _parse_date(raw):
    """football-data.co.uk dates are dd/mm/yy or dd/mm/yyyy depending on
    season; try the modern format first."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognized football-data.co.uk date {raw!r}")


def parse_csv(text, division, with_closing_odds=False):
    """CSV text -> list of records shaped for models/m_dc.py's replay():
    date/home_team/away_team/home_score/away_score/neutral/tournament.
    Skips rows with no final score (postponed/future fixtures).

    `with_closing_odds` is OFF by default, and that default is a guardrail,
    not a convenience. football-data.co.uk's season files carry Pinnacle
    closing prices (PSCH/PSCD/PSCA) from about 2019-20 onward, and this
    function used to attach them unconditionally: a 2026-08-26 audit found
    4010 of 4190 loaded records carrying `pinnacle_close_*`, meaning ANY
    consumer of historical results -- including a price-blind research or
    forensics script that never asked for a price -- was handed market
    numbers it had no business seeing. The import-direction gate in
    audit/selftest.py cannot catch that: it polices which MODULES a file
    imports, not what the DATA quietly contains.

    Callers that legitimately need closing prices (CLV work, a scoring
    backtest against the close) pass `with_closing_odds=True` and thereby
    say so in their own source. Everyone else gets results only."""
    tournament = DIVISIONS.get(division, division)
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        if not row.get("Date") or not row.get("HomeTeam"):
            continue
        hg, ag = row.get("FTHG"), row.get("FTAG")
        if hg in (None, "") or ag in (None, ""):
            continue  # not played yet
        rec = {
            "date": _parse_date(row["Date"]),
            "home_team": row["HomeTeam"].strip(),
            "away_team": row["AwayTeam"].strip(),
            "home_score": int(hg),
            "away_score": int(ag),
            "neutral": False,
            "tournament": tournament,
        }
        if with_closing_odds and all(
                row.get(c) not in (None, "") for c in _PINNACLE_CLOSE_COLS):
            rec["pinnacle_close_h"] = float(row["PSCH"])
            rec["pinnacle_close_d"] = float(row["PSCD"])
            rec["pinnacle_close_a"] = float(row["PSCA"])
        rows.append(rec)
    return rows


def load_seasons(division, start_years, cache_dir=None, force=False,
                 missing_ok=False, with_closing_odds=False,
                 current_season_max_age_hours=6):
    """Fetch+parse multiple seasons (e.g. range(2016, 2026) for 2016-17
    through 2025-26), sorted chronologically, deduped defensively.

    `missing_ok=True` skips seasons the source has not published yet
    (`SeasonNotPublished`) instead of raising -- for callers that include
    the season currently in progress and must still work in the weeks
    before its first match. A season that exists but fails to fetch for any
    other reason still raises: "not published yet" and "the fetch broke"
    must not collapse into the same silent skip."""
    out = []
    current = current_season_start_year()
    for y in start_years:
        # only the in-progress season's file can change; completed seasons
        # are immutable, so re-fetching them would be pure waste
        age = current_season_max_age_hours if y >= current else None
        try:
            text = _fetch_raw(division, y, cache_dir=cache_dir, force=force,
                              max_age_hours=age)
        except SeasonNotPublished:
            if missing_ok:
                continue
            raise
        out.extend(parse_csv(text, division,
                             with_closing_odds=with_closing_odds))
    out.sort(key=lambda r: r["date"])
    return out


def _selfcheck():
    global _fetch_raw
    fails = []

    sample_csv = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA\n"
        "E0,15/08/2025,Liverpool,Bournemouth,4,2,H,1.29,6.25,9\n"
        "E0,16/08/2025,Aston Villa,Newcastle,0,0,D,2.45,3.4,2.8\n"
        "E0,17/08/2025,Postponed,Fixture,,,,,,\n"  # no score -> must skip
    )
    rows = parse_csv(sample_csv, "E0")
    if len(rows) != 2:
        fails.append(f"expected 2 played fixtures parsed (postponed skipped), got {len(rows)}")
    else:
        r0 = rows[0]
        if r0["date"] != "2025-08-15":
            fails.append(f"date parse: expected 2025-08-15, got {r0['date']}")
        if r0["home_team"] != "Liverpool" or r0["away_team"] != "Bournemouth":
            fails.append("team name parse mismatch")
        if (r0["home_score"], r0["away_score"]) != (4, 2):
            fails.append("score parse mismatch")
        if r0["neutral"] is not False:
            fails.append("domestic-league rows must have neutral=False")
        if r0["tournament"] != "English Premier League":
            fails.append("tournament label should resolve via DIVISIONS")
        if "pinnacle_close_h" in r0:
            fails.append("closing odds must NOT ride along by default — a "
                         "price-blind consumer must not be handed prices it "
                         "never asked for")
        if any(k.startswith("pinnacle_") for k in r0):
            fails.append(f"unexpected market keys in a default parse: "
                         f"{sorted(k for k in r0 if k.startswith('pinnacle_'))}")

    priced = parse_csv(sample_csv, "E0", with_closing_odds=True)
    if not priced or priced[0].get("pinnacle_close_h") != 1.29:
        fails.append("with_closing_odds=True should parse the closing prices")

    if season_code(2025) != "2526":
        fails.append(f"season_code(2025) should be '2526', got {season_code(2025)!r}")
    if season_code(1999) != "9900":
        fails.append(f"season_code(1999) should be '9900' (century rollover), got {season_code(1999)!r}")

    # A row using the older two-digit-year date format must still parse.
    old_fmt = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nE0,15/08/09,Everton,Arsenal,1,6\n"
    old_rows = parse_csv(old_fmt, "E0")
    if not old_rows or old_rows[0]["date"] != "2009-08-15":
        fails.append("two-digit-year date format (older seasons) should still parse")

    # the in-progress season's cache must EXPIRE; a completed season's
    # must not. Exercised with a stub so it stays offline.
    import time as _time
    cache_probe = {}

    def _stub_age(division, start_year, cache_dir=None, force=False,
                  max_age_hours=None):
        cache_probe[start_year] = max_age_hours
        return sample_csv

    _real = _fetch_raw
    try:
        _fetch_raw = _stub_age
        cur = current_season_start_year()
        load_seasons("E0", [cur - 3, cur])
        if cache_probe.get(cur - 3) is not None:
            fails.append("a completed season must be cached indefinitely, "
                         f"got max_age_hours={cache_probe.get(cur - 3)}")
        if cache_probe.get(cur) is None:
            fails.append("the in-progress season's cache must expire, or the "
                         "settle job reads a pre-matchday copy all season")
    finally:
        _fetch_raw = _real

    # The source being unwell (5xx / timeout) must not kill a caller that
    # already holds a cached copy; with no copy the error must still surface,
    # and a 404 must stay "not published yet", never be papered over.
    import io as _io
    import tempfile as _tf
    import urllib.error as _ue
    import urllib.request as _ur
    from pathlib import Path as _P
    _real_urlopen = _ur.urlopen
    _err = {"kind": "503"}

    def _boom(req, timeout=None):
        if _err["kind"] == "404":
            raise _ue.HTTPError(req.full_url, 404, "Not Found", {}, _io.BytesIO())
        if _err["kind"] == "503":
            raise _ue.HTTPError(req.full_url, 503, "Unavailable", {}, _io.BytesIO())
        raise _ue.URLError("timed out")

    try:
        _ur.urlopen = _boom
        with _tf.TemporaryDirectory() as td:
            cache = _P(td)
            cached = cache / f"E0_{season_code(2026)}.csv"
            for kind in ("503", "timeout"):
                _err["kind"] = kind
                try:
                    _fetch_raw("E0", 2026, cache_dir=cache, force=True, snapshot_dir=cache / "no-snapshot")
                    fails.append(f"{kind} with no cache must raise, not "
                                 f"return nothing")
                except RuntimeError:
                    pass
            cached.write_text(sample_csv, encoding="utf-8")
            for kind in ("503", "timeout"):
                _err["kind"] = kind
                import contextlib as _cl
                _FALLBACK_SEEN.clear()   # each call must be able to announce itself
                with _cl.redirect_stderr(_io.StringIO()) as warned:
                    got = _fetch_raw("E0", 2026, cache_dir=cache, force=True, snapshot_dir=cache / "no-snapshot")
                if got != sample_csv or "using the cached copy" not in warned.getvalue():
                    fails.append(f"{kind} with a cached copy must serve it "
                                 f"and say so on stderr")
            _err["kind"] = "404"
            try:
                _fetch_raw("E0", 2026, cache_dir=cache, force=True, snapshot_dir=cache / "no-snapshot")
                fails.append("a 404 must stay SeasonNotPublished even when a "
                             "cache exists")
            except SeasonNotPublished:
                pass
    finally:
        _ur.urlopen = _real_urlopen

    # The committed snapshot: price-free by construction, and the fallback of
    # last resort when there is neither network nor a cached copy (a fresh
    # cloud checkout behind a network allowlist).
    with _tf.TemporaryDirectory() as td:
        snap_dir, cache = _P(td) / "snap", _P(td) / "cache"
        if not write_snapshot(sample_csv, "E0", 2025, snapshot_dir=snap_dir):
            fails.append("a first write_snapshot must report a change")
        written = snapshot_path("E0", 2025, snap_dir).read_text(encoding="utf-8")
        if "PSC" in written or "1.29" in written or "Postponed" in written:
            fails.append("the snapshot must carry no odds column and no "
                         "unplayed fixture")
        if write_snapshot(sample_csv, "E0", 2025, snapshot_dir=snap_dir):
            fails.append("re-writing an identical snapshot must report no change")
        if [r["home_team"] for r in parse_csv(written, "E0")] != \
                [r["home_team"] for r in parse_csv(sample_csv, "E0")]:
            fails.append("the snapshot must parse to the same played matches")
        try:
            _ur.urlopen = _boom
            _err["kind"] = "503"
            import contextlib as _cl2
            _FALLBACK_SEEN.clear()
            with _cl2.redirect_stderr(_io.StringIO()) as warned:
                got = _fetch_raw("E0", 2025, cache_dir=cache, snapshot_dir=snap_dir)
            if got != written or "price-free snapshot" not in warned.getvalue():
                fails.append("with no network and no cache, the snapshot must "
                             "be served, and say so on stderr")
            try:
                _fetch_raw("E0", 2025, cache_dir=cache, snapshot_dir=snap_dir,
                           use_snapshot=False)
                fails.append("refreshing the snapshot itself must not read the "
                             "snapshot back")
            except RuntimeError:
                pass
        finally:
            _ur.urlopen = _real_urlopen

    from datetime import date as _date
    for day, expected in ((_date(2026, 8, 31), 2026), (_date(2027, 3, 1), 2026),
                          (_date(2026, 6, 30), 2025)):
        if current_season_start_year(day) != expected:
            fails.append(f"current_season_start_year({day}) should be {expected}")

    # An unpublished season (the season currently in progress, pre-first-
    # match) must be skippable, never fatal -- and must NOT be silently
    # skipped when the caller did not ask for that. Offline: the fetch is
    # stubbed, so this exercises the branch without touching the network.
    real_fetch = _fetch_raw

    def _stub(division, start_year, cache_dir=None, force=False,
              max_age_hours=None):
        if start_year == 2099:
            raise SeasonNotPublished(f"no {division} file for {start_year}")
        return sample_csv

    try:
        _fetch_raw = _stub
        skipped = load_seasons("E0", [2025, 2099], missing_ok=True)
        if len(skipped) != 2:
            fails.append(f"missing_ok=True should skip the unpublished season "
                         f"and keep the published one's 2 rows, got {len(skipped)}")
        try:
            load_seasons("E0", [2025, 2099])
            fails.append("missing_ok=False (default) must propagate "
                         "SeasonNotPublished, not swallow it")
        except SeasonNotPublished:
            pass
    finally:
        _fetch_raw = real_fetch


    # a fallback is announced once per process, however many seasons use it
    import contextlib
    import io as _io
    buf = _io.StringIO()
    saved = dict(_FALLBACK_SEEN)
    _FALLBACK_SEEN.clear()
    try:
        with contextlib.redirect_stderr(buf):
            for yr in range(2015, 2027):
                _warn_fallback("git snapshot", f"WARNING: unreachable for {yr}")
        if buf.getvalue().count("WARNING") != 1:
            fails.append(f"a fallback must be announced once per process, "
                         f"got {buf.getvalue().count('WARNING')} lines")
        if _FALLBACK_SEEN.get("git snapshot") != 12:
            fails.append("the suppressed repeats must still be counted")
    finally:
        _FALLBACK_SEEN.clear()
        _FALLBACK_SEEN.update(saved)

    return fails


def main(argv=None):
    """`python -m football_ai.data.football_data_co_uk` runs the self-check;
    `... refresh-snapshot` refreshes the committed price-free snapshot."""
    import argparse
    ap = argparse.ArgumentParser(description="football-data.co.uk results adapter")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("refresh-snapshot",
                        help="refresh the committed, price-free results snapshot")
    rp.add_argument("--division", default="E0")
    rp.add_argument("--from-year", type=int, default=2015)
    args = ap.parse_args(argv)
    if args.cmd == "refresh-snapshot":
        changed = refresh_snapshots(args.division, args.from_year)
        print(f"results snapshot {args.division}: {len(changed)} file(s) changed"
              + (f" ({', '.join(changed)})" if changed else ""))
        return
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")


if __name__ == "__main__":
    main()
