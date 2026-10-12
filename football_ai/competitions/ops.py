"""Unattended operations for a live competition window: take market
snapshots, settle finished fixtures, and score a finished match.

WHY THIS EXISTS (2026-08-26). The WC2026 window ran these by hand plus 31
Windows scheduled tasks wired to that experiment's own scripts, all of
which were unregistered when it closed. The EPL window inherited none of
it, and the gap showed immediately: the charter names CLV the PRIMARY
yardstick (DECISION_PROTOCOL section 7), CLV needs a CLOSING price, and
nothing existed to take one -- the first live report printed "no closing
snapshot yet" with no way to fix that except a human remembering, at the
right hour, on a Saturday.

Everything here is competition-GENERIC: it takes a CompetitionConfig and a
fixture list, so a second league costs a registry entry, not a rewrite.

DISCIPLINE THIS MODULE KEEPS (it runs unattended, so the rules have to be
in the code rather than in the operator's head):
  * it never PRINTS a price. One `/odds` response covers a whole matchweek,
    and this may run into a log an analyst later reads -- printing prices
    would contaminate every fixture on the sheet at once. Counts and match
    ids only.
  * `closing` is decided per fixture from its own kickoff time, not passed
    in by whoever ran the command, because "was this the closing price" is
    a fact about the clock, not an intention.
  * settling reads the score from the competition's declared results
    source. A score typed in by hand at 2am is exactly the drift the
    ledger audit exists to catch.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from football_ai.core.paths import repo_root
from football_ai.competitions import schedule as _schedule
from football_ai.store import io as store_io

DEFAULT_CLOSING_WINDOW_MIN = 150

# Below this many valid samples, a J is not treated as present for pricing
# purposes and `snapshot` defers the fixture exactly as if it had none.
#
# WHY 2 (2026-09-04). The deferral guard existed to stop a routine price
# grab from destroying the chance to run J, but it asked only "is there a
# `j` key". epl2627_gw03_new_bou carried a J whose k_valid was 1 of 3 --
# the other two samples died on environment failures -- and a J with one
# sample has NO dispersion: `dispersion: 0.0` there does not mean the
# instrument is confident, it means there is no second sample to compare.
# Pricing it froze that permanently, because `amend_lock` then refuses any
# improvement and whoever writes one has by definition seen the price. So
# the chance the guard exists to protect was half lost and the guard did
# not notice.
#
# Two is the principled line, not a tuned one: k=1 is the exact point at
# which the instrument cannot report its own uncertainty at all, which is
# the property `run()` calls "honest uncertainty" and the reason k exists.
# A degraded-but-reportable J (2 of 3) is still a J; a single sample is a
# reading with no error bar.
MIN_K_FOR_PRICING = 2

# A sound J may be re-run ONCE, before any price exists, when the evidence
# it was formed on has since been materially upgraded (owner, 2026-10-10:
# GW06's J were all formed on blocked-network search summaries, 0-5 tier-1a
# claims each, and stayed M forever after a 3-24 tier-1a re-scout). The
# price is still the line: nobody re-running has seen one. "Material" is
# mechanical so it cannot be argued per fixture.
UPGRADE_RERUNS_MAX = 1


def _tier1a(claims):
    return sum(1 for c in claims if str(c.get("source_tier", "")).lower() == "1a")


def _evidence_upgrade(config, match_id, locks, claims):
    """The upgrade that justifies re-running a sound J, or None.

    Compares the new bundle with the evidence stored before the latest
    sound J was locked: tier-1a claims must at least double and grow by 3,
    and the bundle may not shrink. Refused once a sound J has already been
    re-run UPGRADE_RERUNS_MAX times, or when what J saw cannot be found."""
    sound = [l for l in locks if (l["data"]["payload"].get("j") or {})
             and (((l["data"]["payload"]["j"].get("runner") or {}).get("k_valid") or 0)
                  >= MIN_K_FOR_PRICING)]
    if not sound or len(sound) > UPGRADE_RERUNS_MAX:
        return None
    j_ts = sound[-1].get("ts") or ""
    seen = [r["data"] for r in store_io.read("evidence", era=config.era)
            if r.get("match_id") == match_id and (r.get("ts") or "") <= j_ts]
    if not seen:
        return None
    old = seen[-1].get("claims") or []
    o1, n1 = _tier1a(old), _tier1a(claims)
    if n1 >= max(2 * o1, o1 + 3) and len(claims) >= len(old):
        return {"old_tier1a": o1, "new_tier1a": n1,
                "old_claims": len(old), "new_claims": len(claims)}
    return None


def _parse_iso(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def api_key(path=None):
    """the-odds-api key. On the owner's machine it lives outside the tree in
    a gitignored config; on a machine with no such file (a cloud session
    cloned from git) it comes from the ODDS_API_KEY environment variable.
    Either way a missing key is a clear error rather than a confusing HTTP
    401, and the key never enters the repository."""
    p = path or (repo_root() / "history" / "falsification-2026" / "config.json")
    # .strip(): a secret piped in through a shell (PowerShell adds a line
    # ending) or pasted with a trailing newline would otherwise put that
    # newline into every request URL, and every call would quietly fail.
    if p.exists():
        key = json.loads(p.read_text(encoding="utf-8")).get("odds_api_key")
        if not key or not str(key).strip():
            raise KeyError(f"{p} has no 'odds_api_key'")
        return str(key).strip()
    key = (os.environ.get("ODDS_API_KEY") or "").strip()
    if key:
        return key
    raise FileNotFoundError(
        f"no odds-api key: {p} does not exist and ODDS_API_KEY is not set")


def _time_or_none(value):
    """_parse_iso that reads an unreadable stamp as unknown instead of
    raising: the quota gate must still decide, on the side that keeps the
    closing price (an unknown fetch time proves nothing fresh)."""
    try:
        return _parse_iso(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _j_ready(lock_records):
    """{match_id: (usable, locked_at)} for every locked fixture: whether its
    LATEST J carries enough valid samples to count as a J for pricing
    (MIN_K_FOR_PRICING; a J with no runner block is taken at its word), and
    when the lock version holding that J was written (None with no J).

    One definition for both `snapshot` (which defers a fixture without a
    usable J) and `fetch_due` (which calls the API for a fixture WITH one).
    If they disagreed, the gate would call for a fixture snapshot then
    declines to price, on every tick, for nothing."""
    latest = {}
    for rec in lock_records:
        mid = rec.get("match_id")
        if not mid:
            continue
        latest.setdefault(mid, (0, None, None))
        j = (rec["data"].get("payload") or {}).get("j")
        if not j:
            continue
        version = rec.get("version", 1)
        if version >= latest[mid][0]:
            at = _time_or_none(rec["data"].get("locked_at") or rec.get("ts"))
            latest[mid] = (version, j, at)
    out = {}
    for mid, (_, j, at) in latest.items():
        k_valid = ((j or {}).get("runner") or {}).get("k_valid")
        usable = bool(j) and (k_valid is None or k_valid >= MIN_K_FOR_PRICING)
        out[mid] = (usable, at if j else None)
    return out


def fetch_due(fixtures, locks, odds, now):
    """Whether the scheduled job should call the odds API now, and for whom.
    Pure: `locks` and `odds` are the competition's store records (as
    store_io.read returns them), `fixtures` its fixture list.

    Called only when a price is DUE (owner, 2026-10-10; constants in
    `schedule`):
      closing -- a LOCKED fixture kicks off in [now+10, now+45] minutes and
        has no closing-stamped 1x2 snapshot fetched at or after kickoff-60;
      entry -- a fixture with M and a usable J kicks off within 100 hours
        and has no snapshot of any kind.
    An entry is not due again once a call made after its J reached `main`
    failed to price it (`entry_tried`; see schedule.ENTRY_PUSH_SLACK_MIN):
    that fixture is not in the API's response, and each retry would spend
    quota on every tick until kickoff. Its closing call still comes.

    This decides only WHETHER to call. What a call then stores -- every
    locked fixture within the horizon that has a usable J, plus every locked
    fixture inside its closing window -- is `snapshot`'s rule, unchanged."""
    now = now.astimezone(timezone.utc)
    ko_by_id = {f["match_id"]: _parse_iso(f.get("kickoff_utc")) for f in fixtures}
    ready = _j_ready(locks)
    lo, hi = _schedule.CLOSING_DUE_MIN
    fresh = timedelta(minutes=_schedule.CLOSING_FRESH_MIN)
    horizon = timedelta(hours=_schedule.ENTRY_HORIZON_HOURS)
    slack = timedelta(minutes=_schedule.ENTRY_PUSH_SLACK_MIN)

    priced, fresh_close, fetches = set(), {}, set()
    for rec in odds:
        d = rec.get("data") or {}
        mid = rec.get("match_id") or d.get("match_id")
        at = _time_or_none(d.get("fetched_at"))
        priced.add(mid)
        if at is not None:
            fetches.add(at)
            if d.get("market") == "1x2" and d.get("closing"):
                fresh_close[mid] = max(at, fresh_close.get(mid, at))

    closing, entry, tried = [], [], []
    for mid in sorted(ready):
        ko = ko_by_id.get(mid)
        if ko is None or ko <= now:
            continue
        if (now + timedelta(minutes=lo) <= ko <= now + timedelta(minutes=hi)
                and not (mid in fresh_close and fresh_close[mid] >= ko - fresh)):
            closing.append(mid)
        usable, j_at = ready[mid]
        if usable and ko - now <= horizon and mid not in priced:
            if j_at is not None and any(
                    t >= j_at + slack and t < ko and ko - t <= horizon
                    for t in fetches):
                tried.append(mid)
            else:
                entry.append(mid)
    return {"call": bool(closing or entry), "closing_due": closing,
            "entry_due": entry, "entry_tried": tried}


def snapshot(config, fixtures, sport_key, key=None, now=None,
             closing_window_min=DEFAULT_CLOSING_WINDOW_MIN,
             bookmakers="pinnacle", regions="eu", markets=None,
             only_within_hours=None, when_due=False):
    """Fetch one market snapshot and store every locked fixture's records.

    Returns a summary dict with COUNTS ONLY -- deliberately no prices, see
    the module docstring. A fixture kicking off within
    `closing_window_min` minutes is stamped `closing=True`, which is what
    `market/clv.py` looks for.

    `only_within_hours` RESTRICTS which fixtures get stored, and it is the
    setting that matters most for an automated job. One `/odds` response
    covers every upcoming fixture in the competition, so storing all of
    them means every fixture on the sheet now has a price in the store --
    and `amend_lock` then refuses to write J for any of them. A fixture
    three weekends out would silently lose its J option to a routine
    closing grab for this weekend's games. Pass a window (the scheduled
    closing job uses a few hours) so the job touches only what it is
    actually there to price.

    `when_due` makes the call itself conditional (fetch_due): with nothing
    due, it returns before any network call. With something due, the
    summary's `due_not_priced` names any due fixture the call failed to
    price.
    """
    from football_ai.data import the_odds_api as api

    now = now or datetime.now(timezone.utc)

    # The due check, BEFORE the network call (`when_due`, what the scheduled
    # job passes). The job ticks every 20 minutes through the day; without
    # this every tick would spend quota. With it a tick calls only when a
    # closing or an entry price is owed (fetch_due), so most ticks cost
    # nothing and need no key.
    due = None
    if when_due:
        due = fetch_due(fixtures, store_io.read("locks", era=config.era),
                        store_io.read("odds", era=config.era), now)
        if not due["call"]:
            tried = (f"; entry not retried (an earlier call did not price "
                     f"it): {', '.join(due['entry_tried'])}"
                     if due["entry_tried"] else "")
            return {"stored": 0, "fixtures": [], "closing_fixtures": [],
                    "refused": [], "excluded_by_window": [],
                    "quota_remaining": None, "raw": None, "due": due,
                    "due_not_priced": [],
                    "skipped": (f"no closing or entry price due - no API call "
                                f"made, no quota spent{tried}")}

    key = key or api_key()
    kwargs = {"bookmakers": bookmakers, "regions": regions}
    if markets:
        kwargs["markets"] = markets
    events, quota, raw_path = api.fetch_odds(sport_key, key, **kwargs)

    # NOTE the full fixture list goes to to_snapshots, and the horizon is
    # applied to the RESULTING RECORDS. Filtering the fixture list first
    # looked equivalent and was not: every windowed-out fixture then came
    # back as "unknown fixture", which is the message reserved for a real
    # club-name mapping failure. In an unattended log that buried the one
    # line a human must act on under ten that mean "working as intended".
    recs, skipped = api.to_snapshots(events, fixtures, era=config.era,
                                     raw_name=raw_path.name)

    ko_by_id = {f["match_id"]: _parse_iso(f.get("kickoff_utc"))
                for f in fixtures}
    horizon = (now + timedelta(hours=only_within_hours)
               if only_within_hours is not None else None)

    # Fixtures locked with no J yet. Pricing one of these FORECLOSES its J
    # permanently: amend_lock refuses a J written after a price exists, so a
    # routine mid-week grab for this weekend's games silently kills next
    # weekend's judgment work. It has happened once already
    # (epl2627_gw03_ips_liv, priced 2026-08-31 at T-98.9h by the closing run
    # for a different fixture) and the horizon setting did not prevent it --
    # a knob nobody re-checks is not a guard, so the rule lives here.
    #
    # The closing window is the deliberate exception: CLV needs a closing
    # price for every fixture including M-only ones, and by T-2.5h the
    # chance to run J has passed anyway. So: never price an un-J'd fixture
    # EARLY, always price it at the close.
    # A fixture counts as J'd only if its J carried enough valid samples to
    # report its own dispersion (MIN_K_FOR_PRICING). A one-sample J is a
    # reading with no error bar, and pricing it makes that permanent.
    # Judge the LATEST J, not "any version ever had one" -- an amend that
    # replaces a J is the reading that counts, the same rule `review()`
    # follows. ORing across versions would let a superseded J vouch for the
    # one actually in force. (_j_ready, shared with fetch_due.)
    j_by_id = {mid: usable for mid, (usable, _) in
               _j_ready(store_io.read("locks", era=config.era)).items()}

    stored, closing_ids, excluded, deferred = 0, set(), set(), set()
    stored_ids, closing_1x2 = set(), set()
    for r in recs:
        mid = r["match_id"]
        ko = ko_by_id.get(mid)
        if horizon is not None and ko is not None and ko > horizon:
            excluded.add(mid)
            continue
        is_closing = (ko is not None
                      and now <= ko <= now + timedelta(minutes=closing_window_min))
        if not is_closing and mid in j_by_id and not j_by_id[mid]:
            deferred.add(mid)
            continue
        if is_closing:
            r["closing"] = True
            closing_ids.add(mid)
            if r.get("market") == "1x2":
                closing_1x2.add(mid)
        store_io.append("odds", r, era=config.era, match_id=mid)
        stored_ids.add(mid)
        stored += 1

    # A price the gate called for and did not get: the fixture is missing
    # from the response (club-name mapping, postponement, no line yet) or
    # was windowed out. Reported, and made a failure by `main`, because a
    # missed closing price is a CLV gap nobody would otherwise see.
    due_not_priced = []
    if due is not None:
        due_not_priced = sorted(
            {m for m in due["closing_due"] if m not in closing_1x2}
            | {m for m in due["entry_due"] if m not in stored_ids})

    return {
        "due": due,
        "due_not_priced": due_not_priced,
        "stored": stored,
        # fixtures actually STORED -- not merely "seen and not windowed out".
        # Reporting a deferred fixture here would tell the operator its price
        # was captured when it deliberately was not.
        "fixtures": sorted({r["match_id"] for r in recs
                            if r["match_id"] not in excluded
                            and r["match_id"] not in deferred}),
        "closing_fixtures": sorted(closing_ids),
        "refused": [f"{reason}: {desc}" for reason, desc in skipped],
        "excluded_by_window": sorted(excluded),
        "deferred_no_j": sorted(deferred),
        "quota_remaining": quota.get("x-requests-remaining"),
        "raw": raw_path.name,
    }


def _preserve_j(match_id, j):
    """Write J's result to a file outside the repo; return its path."""
    import tempfile
    d = Path(tempfile.gettempdir()) / "football_ai_j"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{match_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    path.write_text(json.dumps(j, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    return str(path)


def store_evidence(config, match_id, claims, dry_run=False, source="ops.evidence",
                   scout_reports=None):
    """Validate an evidence bundle and keep it in the store, once.

    The validated bundle IS the fixture's evidence. Until 2026-10-06 it was
    used by `run_j` and then discarded: nine fixtures' J rested on 88-115
    claims each that exist nowhere in the store, so no factor's evidence_refs
    could be checked and no `absorption` tag could ever be scored against what
    the market did. And a fixture priced before its J was locked had no path
    that stored evidence at all, although its judgment still needs it; so
    storing is a function of its own, and `judgment.record` reads what it
    wrote. Keyed by content hash, so a retry records the bundle once.

    Refuses a bundle with a schema error or a market leak (the message names
    claims and kinds, never the flagged text).

    `scout_reports` is what `ops_tools.load_scout_reports` read from the
    scouts' `.scout.json` files: whether each scout saw market numbers, could
    open pages, ran out of searches. Until 2026-10-07 that existed only in the
    orchestrating conversation and died with it, so nobody downstream could
    tell a bundle built by an exposed scout from a clean one, or from one
    built on search snippets alone. It goes into the record next to the
    claims. The loader already withholds any free text with figures in it;
    a report text with a market number is still refused here, as the last
    guard before the ledger."""
    from football_ai.evidence.schema import coverage, refusal_text, validate_bundle
    bundle = validate_bundle(claims, categories=list(config.evidence_categories),
                             require_dimension=True)
    if bundle["errors"] or bundle["leaks"]:
        raise ValueError(
            f"REFUSED: evidence bundle for {match_id!r} did not pass "
            f"mechanical validation: {refusal_text(bundle)}.")
    from football_ai.evidence import firewall
    leaky = [r.get("file") for r in (scout_reports or {}).get("reports") or []
             if isinstance(r.get("market_content"), str)
             and firewall.leaked_numbers(r["market_content"])]
    if leaky:
        raise ValueError(
            f"REFUSED: the scout report for {', '.join(map(str, leaky))} carries a "
            f"market-derived number in market_content; load it with "
            f"ops_tools.load_scout_reports, which withholds such text.")
    claims_sha = hashlib.sha256(json.dumps(
        claims, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    stored = False
    reports = (None if scout_reports is None else
               {"reports": list(scout_reports.get("reports") or []),
                "missing": list(scout_reports.get("missing") or [])})
    rows = [(r.get("data") or {}) for r in store_io.read("evidence", era=config.era)
            if r.get("match_id") == match_id]
    same = [d for d in rows if d.get("claims_sha256") == claims_sha]
    if reports is None or not reports["reports"]:
        # no reports to add: the same claims are stored once, and a re-store
        # without reports (run-j on copied files) never hides reports on record
        duplicate = bool(same)
    else:
        # readers take the LATEST row, so compare with that one: a report the
        # scout wrote or fixed late is a new row, and R1, R2, R1 ends at R1
        latest = rows[-1] if rows else {}
        duplicate = (latest.get("claims_sha256") == claims_sha
                     and latest.get("scout_reports") == reports)
    if not dry_run and not duplicate:
        record = {"match_id": match_id, "claims": claims,
                  "claims_sha256": claims_sha,
                  "firewall": (f"validate_bundle: {len(bundle['errors'])} errors, "
                               f"{len(bundle['leaks'])} leaks"),
                  "coverage": coverage(claims), "source": source}
        if reports is not None:
            record["scout_reports"] = reports
        store_io.append("evidence", record, era=config.era, match_id=match_id)
        stored = True
    return {"claims_sha256": claims_sha, "evidence_stored": stored,
            "coverage": coverage(claims)}


def run_j(config, match_id, claims, amend, goal_curve=None, llm=None, k=3,
          timeout=None, dry_run=False, j_model=None, scout_reports=None):
    """Run instrument J for one locked fixture and amend it into the lock.

    WHY THIS IS CODE (2026-09-03). Every session so far assembled this
    sequence by hand from the runbook, and the hand-assembly is where the
    damage came from: the skill documented the goal-curve key as `rho` when
    `j_runner.implied_probs_from_factors` reads `dixon_coles_rho`, so the
    one live J ever run had its arithmetic gate silently switched off and
    came back `checked: False` -- a claim about the model rather than a
    reading of it, and nobody could tell from the record. Steps that must
    happen in one order, with guards that must not be skipped, belong in a
    function rather than in a runbook a human retypes each week.

    The order, and what each step refuses:

      1. the fixture must have a base lock and must not already carry a J;
      2. **no odds snapshot may exist for it** -- once a price is in the
         store, `amend` will refuse anyway, but failing here says WHY
         (J is foreclosed for this fixture, permanently and non-randomly)
         instead of surfacing as a generic amend error;
      3. the bundle passes `evidence.schema.validate_bundle` -- schema AND
         the market-leak firewall. A bundle with any error or leak never
         reaches the model;
      4. `inputs.goal_curve` is filled in when the lock predates it, under
         the key names the arithmetic gate actually reads;
      5. J runs isolated (`j_runner.run` -> `cli_llm` in a subprocess with
         no tools and a temp cwd); this session only orchestrates;
      6. an arithmetic gate that did not run is an ERROR, not a shrug --
         that is precisely the failure this function exists to stop;
      7. only then is the lock amended.

    `claims` is the validated evidence bundle (list of claim dicts).
    `amend` is the competition's `amend_lock`; `goal_curve` is a
    `() -> (g_a, g_b, rho)` callable used only when the lock lacks the
    curve. `dry_run` does everything except write.

    `j_model` is an optional `--model` for the J subprocess; None leaves it
    to the CLI's default. Either way the model that actually answered is
    recorded in `j.runner.models` and named in the amend reason, because the
    instrument is spec x bundle x M x model and only the first three used to
    be on record (2026-10-06). Nothing here restricts WHICH model: it only
    makes the choice attributable afterwards.

    Returns a summary dict. It carries J's probabilities (an instrument
    reading, not a market number) but never a price.
    """
    import copy
    upgrade = None
    from football_ai.evidence.schema import (coverage, refusal_text,
                                             validate_bundle)
    from football_ai.research import j_runner
    from football_ai.store import sync as store_sync

    # the odds guard below reads the LOCAL odds file: refuse to run on a
    # checkout that is behind origin (see store/sync.py for why)
    store_sync.assert_current()

    locks = sorted([r for r in store_io.read("locks", era=config.era)
                    if r.get("match_id") == match_id],
                   key=lambda r: r.get("version", 1))
    if not locks:
        raise ValueError(f"REFUSED: {match_id!r} has no lock in era "
                         f"{config.era!r} -- lock M first")
    existing_j = next((l["data"]["payload"].get("j") for l in reversed(locks)
                       if l["data"]["payload"].get("j")), None)
    if existing_j is not None:
        k_valid = (existing_j.get("runner") or {}).get("k_valid")
        degraded = k_valid is not None and k_valid < MIN_K_FOR_PRICING
        # A DEGRADED J on an UNPRICED fixture may be re-run. Nothing is
        # contaminated: no price exists for anyone to have seen, and the
        # samples were lost to the environment (quota, timeouts), not to
        # judgment -- AGENT_DISPATCH section 5 says fix the tool and retry
        # that class of failure. The old version stays in the ledger; this
        # appends a better one beside it rather than replacing it.
        #
        # The line is drawn at the PRICE, not at whether the output was
        # seen. Once a price exists (epl2627_gw03_new_bou), whoever writes a
        # replacement knows which direction would make the fixture
        # interesting, and re-running becomes rolling until satisfied.
        upgrade = None if degraded else _evidence_upgrade(config, match_id, locks, claims)
        if not degraded and upgrade is None:
            raise ValueError(
                f"REFUSED: {match_id!r} already carries a J with "
                f"k_valid={k_valid} -- rerunning would overwrite a sound "
                f"locked estimate. A re-run is allowed once, before any "
                f"price, only when the new bundle at least doubles the "
                f"tier-1a claims J saw (and adds 3) without shrinking")
        # the odds check below is what makes this safe; it runs next.

    if any(r.get("match_id") == match_id
           for r in store_io.read("odds", era=config.era)):
        raise ValueError(
            f"REFUSED: {match_id!r} already has an odds snapshot, so a clean "
            f"J is FORECLOSED for it permanently -- any estimate written now "
            f"may have seen the price (the m97 lesson). This fixture is "
            f"M-only for the rest of its life; record the loss as a "
            f"non-random gap rather than writing a contaminated J")

    bundle = validate_bundle(claims, categories=list(config.evidence_categories),
                             require_dimension=True)
    if bundle["errors"] or bundle["leaks"]:
        raise ValueError(
            f"REFUSED: evidence bundle for {match_id!r} did not pass "
            f"mechanical validation: {refusal_text(bundle)}. Send those "
            f"claims back to the scout to be rewritten without the market "
            f"number; the flagged text is deliberately not shown here -- do "
            f"not go looking for it and do not bypass the validator.")

    # The validated bundle IS this fixture's evidence (see store_evidence).
    ev = store_evidence(config, match_id, claims, dry_run=dry_run,
                        source="ops.run_j", scout_reports=scout_reports)
    claims_sha, evidence_stored = ev["claims_sha256"], ev["evidence_stored"]

    payload = copy.deepcopy(locks[0]["data"]["payload"])
    curve_added = False
    inputs = payload.setdefault("inputs", {})
    if not inputs.get("goal_curve"):
        if goal_curve is None:
            raise ValueError(
                f"REFUSED: {match_id!r} was locked before locks carried "
                f"inputs.goal_curve, and no goal_curve provider was passed. "
                f"Running without it turns J's arithmetic gate off silently")
        g_a, g_b, rho = goal_curve()
        # these three key names are read by
        # j_runner.implied_probs_from_factors; writing `rho` instead of
        # `dixon_coles_rho` is what silently disabled the gate before
        inputs["goal_curve"] = {"g_a": g_a, "g_b": g_b,
                                "dixon_coles_rho": rho}
        curve_added = True

    # timeout=None lets cli_llm derive the budget from the prompt itself
    # (j_runner.suggested_timeout) instead of a constant calibrated on a
    # smaller bundle -- the failure that killed three bri_lee samples 48
    # seconds short of finishing, twice.
    llm = llm or (lambda prompt: j_runner.cli_llm(prompt, timeout=timeout,
                                                  model=j_model))
    j = j_runner.run(payload, claims, llm=llm, k=k,
                     spec_version=config.j_spec_version,
                     model_requested=j_model)
    # J costs ten-odd minutes. Put the result somewhere safe BEFORE anything
    # can refuse it (the arithmetic gate, or an amend that is refused because
    # the checkout fell behind origin or a price landed meanwhile): losing it
    # costs another full run against a deadline. It is outside the repo, it is
    # not the ledger, and re-applying it still goes through amend's guards.
    scratch = _preserve_j(match_id, j)

    arithmetic = j["runner"].get("arithmetic") or {}
    if arithmetic.get("checked") is False:
        raise ValueError(
            f"REFUSED: J for {match_id!r} came back with its arithmetic gate "
            f"NOT run ({arithmetic.get('note')}). An unchecked J is a claim "
            f"about the goal model, not a reading of it -- fix the payload "
            f"and rerun rather than locking it")

    payload["j"] = j
    cov = coverage(claims)
    models_txt = ", ".join(j["runner"]["models"]) or "model not reported"
    reason = (f"instrument J (spec {j['runner']['j_spec_version']}, "
              f"model {models_txt}, "
              f"k={j['runner']['k_valid']}/{j['runner']['k_requested']} valid) "
              f"from a firewall-validated bundle of {len(claims)} claims "
              f"covering {len(cov['covered'])}/10 dimensions")
    if upgrade:
        reason += (f"; evidence-upgrade re-run of a sound J (tier-1a "
                   f"{upgrade['old_tier1a']} -> {upgrade['new_tier1a']}, claims "
                   f"{upgrade['old_claims']} -> {upgrade['new_claims']}), before any price")
    try:
        written = None if dry_run else amend(match_id, payload, reason)
    except Exception:
        print(f"J result for {match_id} was formed but NOT written to the "
              f"ledger (the amend was refused); it is preserved at {scratch}",
              file=sys.stderr)
        raise

    return {
        "match_id": match_id,
        "dry_run": dry_run,
        "version": (written or {}).get("version"),
        "goal_curve_added": curve_added,
        "evidence_stored": evidence_stored,
        "claims_sha256": claims_sha,
        "j_probs_90": j["probs_90"],
        "j_scratch": scratch,
        "m_probs_90": locks[0]["data"]["payload"]["probs_90"],
        "factors": len(j.get("factors") or []),
        "mechanism_types": [f.get("mechanism_type") for f in (j.get("factors") or [])],
        "k_valid": j["runner"]["k_valid"],
        "models": j["runner"]["models"],
        "rejects": j["runner"]["rejects"],
        "dispersion": j["runner"]["dispersion"],
        "arithmetic_max_delta_pp": arithmetic.get("max_delta_pp"),
        "claims": len(claims),
        "coverage_silent": cov["silent"],
    }


def _locked_unsettled(config, now=None):
    """(match_id, kickoff, payload) for fixtures locked, kicked off, and
    with no result recorded."""
    now = now or datetime.now(timezone.utc)
    settled = {r["data"].get("match_id")
               for r in store_io.read("results", era=config.era)
               if "score90" in r["data"]}
    best = {}
    for r in store_io.read("locks", era=config.era):
        mid = r.get("match_id")
        if not mid or mid in settled:
            continue
        if r.get("version", 1) >= best.get(mid, (0, None))[0]:
            best[mid] = (r.get("version", 1), r["data"]["payload"])
    out = []
    for mid, (_, payload) in best.items():
        ko = _parse_iso(payload.get("kickoff_utc"))
        if ko is not None and ko < now:
            out.append((mid, ko, payload))
    return sorted(out, key=lambda t: t[1])


def settle_pending(config, now=None, dry_run=False, start_years=None):
    """Settle every locked, kicked-off, unsettled fixture from the
    competition's DECLARED results source.

    Never invents or accepts a hand-typed score: a fixture whose result the
    source does not yet carry is reported as pending and left alone.
    """
    from football_ai.data.football_data_co_uk import load_seasons

    source = config.data_sources.results_source or ""
    if not source.startswith("football_data_co_uk:"):
        raise NotImplementedError(
            f"no settle adapter for results_source {source!r}")
    division = source.split(":", 1)[1]

    pending = _locked_unsettled(config, now=now)
    if not pending:
        return {"settled": [], "pending": [], "checked": 0}

    if start_years is None:
        years = sorted({_parse_iso(p["kickoff_utc"]).year for _, _, p in pending})
        start_years = sorted({y if _parse_iso(p["kickoff_utc"]).month >= 7 else y - 1
                              for (_, _, p), y in zip(pending, years)})
    rows = load_seasons(division, start_years, missing_ok=True)
    by_pair = {}
    for r in rows:
        by_pair.setdefault((r["home_team"], r["away_team"]), []).append(r)

    settled, still_pending = [], []
    for mid, ko, payload in pending:
        teams = payload.get("teams") or {}
        cands = by_pair.get((teams.get("home"), teams.get("away")), [])
        # a league pairing repeats once per season; take the one whose date
        # is nearest this fixture's kickoff, and only if it is close
        match = None
        for r in cands:
            delta = abs((datetime.fromisoformat(r["date"] + "T00:00:00+00:00")
                         - ko).total_seconds())
            if delta <= 3 * 86400:
                match = r
                break
        if match is None:
            still_pending.append(f"{mid}: source has no result yet")
            continue
        if not dry_run:
            store_io.append("results",
                            {"match_id": mid,
                             "score90": [match["home_score"], match["away_score"]],
                             "source": f"{source} {match['date']}"},
                            era=config.era, match_id=mid)
        settled.append(f"{mid}: {match['home_score']}-{match['away_score']}"
                       f" ({match['date']})")
    return {"settled": settled, "pending": still_pending,
            "checked": len(pending), "dry_run": dry_run}


def _shadow_clv(readings, snaps, kickoff_utc, preferred_book):
    """Per-layer closing-line value: (result, reason_it_is_absent).

    CLV is the charter's PRIMARY yardstick (DECISION_PROTOCOL section 7),
    and until now `review()` computed RPS and Brier and not this one -- the
    two metrics the charter files under "informational" were the only ones
    a settlement produced. RPS also needs roughly 400 matches to separate
    two instruments; CLV is the small-sample proxy, which is exactly what a
    thirty-match window has.

    No bet was placed, so what is measured is a SHADOW entry: for each
    layer, the side on which that layer most disagrees with the earliest
    real pre-close quote, valued at that real quote against the Shin-
    devigged close. Every layer is measured off the SAME entry snapshot, so
    the J-minus-M and judgment-minus-J differentials isolate the layers'
    own probabilities rather than differences in when someone looked.

    Returns (None, reason) rather than a zero when the inputs are not
    there -- a fixture with only a closing price has no value to measure,
    and reporting 0.0 for it would enter a fabrication into the ledger.
    """
    from football_ai.market import clv as clv_mod
    from football_ai.market.devig import devig_shin

    close = (clv_mod.closing_snapshot(snaps, book=preferred_book,
                                      kickoff_utc=kickoff_utc)
             or clv_mod.closing_snapshot(snaps, book=None,
                                         kickoff_utc=kickoff_utc))
    if close is None:
        return None, "no closing snapshot for this fixture"
    entries = [s for s in snaps if not s.get("closing")]
    if not entries:
        return None, ("only a closing price was ever captured, so there is "
                      "no entry price to measure value against")
    preferred = [s for s in entries if s.get("book") == preferred_book]
    entry = min(preferred or entries, key=lambda s: s["fetched_at"])
    fair_entry = dict(zip(entry["outcomes"], devig_shin(entry["odds"])))
    odds_by_outcome = dict(zip(entry["outcomes"], entry["odds"]))

    out = {}
    for name, p in readings.items():
        gaps = {o: p[o] - fair_entry.get(o, 0.0) for o in ("home", "draw", "away")}
        side = max(gaps, key=lambda o: gaps[o])
        taken = odds_by_outcome.get(side)
        out[name] = {
            "side": side,
            "gap_pp": gaps[side] * 100.0,
            "entry_book": entry.get("book"),
            "entry_fetched_at": entry.get("fetched_at"),
            "entry_odds": taken,
            "clv": clv_mod.clv(taken, side, close) if taken else None,
        }
    return {"close_book": close.get("book"),
            "close_is_flagged": bool(close.get("closing")),
            "per_layer": out}, None


def review(config, match_id):
    """Score one settled fixture across every reading that was recorded.

    Deliberately NOT a verdict on whether the call was right: the charter
    forbids evaluating a single match's outcome (DECISION_PROTOCOL section
    7, "永不評估:單場猜中與否"). What this produces is the per-reading
    RPS/Brier line for the LEDGER, so it can accumulate into something with
    an n, plus CLV, which is the actual primary yardstick.
    """
    from football_ai.decision import judgment
    from football_ai.market.consensus import snap_fair
    from football_ai.research.metrics import brier, rps

    res = next((r["data"] for r in store_io.read("results", era=config.era)
                if r["data"].get("match_id") == match_id
                and "score90" in r["data"]), None)
    if res is None:
        raise ValueError(f"{match_id} is not settled yet")
    hs, as_ = res["score90"]
    outcome_idx = 0 if hs > as_ else (1 if hs == as_ else 2)

    locks = sorted([r for r in store_io.read("locks", era=config.era)
                    if r.get("match_id") == match_id],
                   key=lambda r: r.get("version", 1))
    if not locks:
        raise ValueError(f"{match_id} has no lock")
    readings = {}
    m_payload = locks[0]["data"]["payload"]
    readings["M"] = m_payload["probs_90"]
    j_block = next((l["data"]["payload"].get("j") for l in reversed(locks)
                    if l["data"]["payload"].get("j")), None)
    # an amend accepted AFTER market prices existed carries this flag; the
    # J it holds is not an independent reading and must say so in the ledger
    j_contaminated = any(l["data"].get("contaminated_post_odds") for l in locks)
    if j_block and j_block.get("probs_90"):
        readings["J"] = j_block["probs_90"]
    jd = next((r["data"] for r in reversed(store_io.read("judgments", era=config.era))
               if r["data"].get("match_id") == match_id), None)
    if jd and jd.get("judgment_probs_90"):
        readings["judgment"] = jd["judgment_probs_90"]

    snaps = [r["data"] for r in store_io.read("odds", era=config.era)
             if r.get("match_id") == match_id
             and r["data"].get("market") == "1x2"]
    close = [s for s in snaps if s.get("closing")]
    used = close or snaps
    market_fair = None
    if used:
        latest = max(used, key=lambda s: s["fetched_at"])
        fair = snap_fair(latest)  # {outcome: devigged prob}
        market_fair = {k: fair[k] for k in ("home", "draw", "away")}
        readings[f"market({latest['book']}"
                 f"{', CLOSING' if latest.get('closing') else ', not closing'})"] = market_fair

    scored = {}
    for name, p in readings.items():
        vec = [p["home"], p["draw"], p["away"]]
        scored[name] = {"probs": vec, "rps": rps(vec, outcome_idx),
                        "brier": brier(vec, outcome_idx)}

    # The judgment layer's COMMITTED LEAN, not just its distribution
    # (DECISION_PROTOCOL section 4 makes the lean the mandatory deliverable; the
    # distribution is only what it was derived from). Surfaced here because
    # a lean that is never read back after settlement cannot be scored, and
    # scoring is the only way to find out whether the layer earns its cost.
    # `decision/judgment.py` defines and validates these fields; a
    # free-format legacy row simply reports whatever of them it has.
    lean = None
    if jd:
        lean = {k: jd.get(k) for k in
                ("direction", "prob", "confidence", "lambda", "f_star",
                 "stake_frac", "j_status", "diverges_from_j",
                 "divergence_note", "outcome", "model", "reviewer_models")}
        lean["hit"] = (None if not jd.get("direction")
                       else jd["direction"] == ["home", "draw", "away"][outcome_idx])
        lean["schema_errors"] = judgment.validate_record(jd)

    # CLV, the charter's PRIMARY yardstick -- computed only over the
    # INSTRUMENT layers; the market reading is the benchmark, and scoring it
    # against itself would produce a number that means nothing.
    instrument_readings = {k: v for k, v in readings.items()
                           if not k.startswith("market(")}
    clv_block, clv_reason = _shadow_clv(
        instrument_readings, snaps, m_payload.get("kickoff_utc"),
        config.data_sources.odds_book_default)

    # The differentials are the point: M's CLV is already known to be
    # negative on this window's own out-of-time sample, so what is being
    # asked is whether each layer ADDS anything over the one below it.
    differentials = {}
    if clv_block:
        per = clv_block["per_layer"]

        def _d(a, b):
            if a in per and b in per and per[a]["clv"] is not None \
                    and per[b]["clv"] is not None:
                return per[a]["clv"] - per[b]["clv"]
            return None
        differentials = {"J_minus_M": _d("J", "M"),
                         "judgment_minus_J": _d("judgment", "J"),
                         "judgment_minus_M": _d("judgment", "M")}

    # Each J factor stated, AT LOCK TIME, what observable fact would show it
    # had materialised. Deciding after the match what counts as
    # materialisation is hindsight and is not a check, which is why the
    # field is mandatory at lock time -- and why it has to be READ here.
    # The verdict is left None: a human settles it, then writes it back to
    # the evidence claim's `materialized` field.
    materialisation = [
        {"mechanism_type": f.get("mechanism_type"),
         "direction": f.get("direction"),
         "lever": f.get("lever"),
         "check": f.get("materialisation_check"),
         "verdict": None}
        for f in ((j_block or {}).get("factors") or [])]

    return {
        "match_id": match_id,
        "teams": m_payload.get("teams"),
        "score90": [hs, as_],
        "outcome": ["home", "draw", "away"][outcome_idx],
        "readings": scored,
        "j_contaminated_post_odds": j_contaminated,
        # who produced each instrument reading; None = the row predates
        # model recording (J before 2026-10-06, judgments before it) and
        # cannot be recovered
        "instrument_models": {
            "J": ((j_block or {}).get("runner") or {}).get("models") or None,
            "judgment": (jd or {}).get("model"),
            "judgment_reviewers": (jd or {}).get("reviewer_models"),
        },
        "judgment_lean": lean,
        "clv": clv_block,
        "clv_absent_because": clv_reason,
        "clv_differentials": differentials,
        "materialisation": materialisation,
        "closing_snapshot_available": bool(close),
        "caveat": ("single-match RPS/Brier is a LEDGER ENTRY, never a "
                   "verdict — the charter forbids evaluating one match's "
                   "outcome. Accumulate before claiming anything."),
        "todo": [
            "judge each entry in `materialisation` yes/no/opposite against "
            "the check it stated at lock time, then write the verdict to "
            "the corresponding evidence claim's `materialized` field",
            "fill `outcome` on this match's judgments record",
        ],
    }


VERDICTS = ("yes", "no", "opposite", "undeterminable")


def _j_factors(config, match_id):
    """(lock version, factors) of the latest J on this fixture's lock."""
    locks = sorted([r for r in store_io.read("locks", era=config.era)
                    if r.get("match_id") == match_id],
                   key=lambda r: r.get("version", 1))
    for rec in reversed(locks):
        j = (rec["data"].get("payload") or {}).get("j")
        if j:
            return rec.get("version", 1), list(j.get("factors") or [])
    return None, []


def materialisation_pending(config):
    """Settled fixtures whose J factors still lack a verdict in the
    mechanism ledger: [(match_id, [factor indices])]."""
    settled = {r["data"].get("match_id") for r in store_io.read("results", era=config.era)
               if "score90" in r["data"]}
    judged = {(r["data"].get("match_id"), r["data"].get("factor_index"))
              for r in store_io.read("mechanisms", era=config.era)}
    out = []
    for mid in sorted(settled):
        _, factors = _j_factors(config, mid)
        missing = [i for i in range(len(factors)) if (mid, i) not in judged]
        if missing:
            out.append((mid, missing))
    return out


def record_materialisation(config, match_id, verdicts, judged_by,
                           supersede=False, dry_run=False):
    """Append one mechanism-ledger row per judged J factor.

    THE MISSING WRITER (2026-10-10). Every J factor states at lock time what
    observable fact would show it materialised (`materialisation_check`),
    and `sizing.precedent_stats` reads this ledger to set lambda_precedent --
    but nothing ever wrote EPL rows, so every class stayed at n=0 forever.

    `verdicts`: [{"factor_index", "verdict": yes|no|opposite|undeterminable,
    "basis": what was observed and where}]. The check judged is the one
    copied from the lock, never a restatement. Only yes/no/opposite count
    as precedents; "undeterminable" is recorded so the gap is visible.
    A second verdict on the same factor is refused unless `supersede`, and
    then the latest row is the one read (precedent_stats de-duplicates)."""
    if not any(r["data"].get("match_id") == match_id and "score90" in r["data"]
               for r in store_io.read("results", era=config.era)):
        raise ValueError(f"REFUSED: {match_id!r} is not settled; a materialisation "
                         f"verdict before the match is a forecast, not a check")
    version, factors = _j_factors(config, match_id)
    if not factors:
        raise ValueError(f"REFUSED: {match_id!r} has no J factors to judge")
    if not judged_by:
        raise ValueError("REFUSED: judged_by must name who judged (a model or a person)")
    already = {r["data"].get("factor_index") for r in store_io.read("mechanisms", era=config.era)
               if r["data"].get("match_id") == match_id}
    rows = []
    for v in verdicts:
        i = v.get("factor_index")
        if not isinstance(i, int) or not (0 <= i < len(factors)):
            raise ValueError(f"REFUSED: factor_index {i!r} out of range 0..{len(factors) - 1}")
        if v.get("verdict") not in VERDICTS:
            raise ValueError(f"REFUSED: verdict must be one of {VERDICTS}, got {v.get('verdict')!r}")
        if not str(v.get("basis") or "").strip():
            raise ValueError(f"REFUSED: factor {i} has no basis; say what was observed and where")
        if i in already and not supersede:
            raise ValueError(f"REFUSED: factor {i} of {match_id!r} is already judged; "
                             f"pass supersede to append a correction")
        f = factors[i]
        rows.append({"match_id": match_id, "source": "J factor", "factor_index": i,
                     "j_lock_version": version,
                     "mechanism_types": [f.get("mechanism_type")],
                     "direction": f.get("direction"), "lever": f.get("lever"),
                     "check": f.get("materialisation_check"),
                     "materialized": v["verdict"], "basis": v["basis"].strip(),
                     "judged_by": judged_by, "supersedes": bool(i in already)})
    if not dry_run:
        for row in rows:
            store_io.append("mechanisms", row, era=config.era, match_id=match_id)
    return {"match_id": match_id, "written": 0 if dry_run else len(rows),
            "dry_run": dry_run, "verdicts": {r["factor_index"]: r["materialized"] for r in rows}}


def _attribute_models(locks, judgments):
    """Which model produced each J and each judgment, from raw store rows.

    Buckets, per layer: a model name (several joined with '+' if one J's
    samples were answered by more than one), "unrecorded" for rows that
    predate model recording and cannot be recovered, and for judgments
    "undeclared" when the writer did not say. `mixed` is True when more than
    one real model appears in a layer: a difference between layers then
    cannot be attributed to the layer. `reviewer_same_as_producer` lists
    judgments whose own model also sat among their reviewers."""
    latest_j = {}
    for r in sorted(locks, key=lambda r: r.get("version", 1)):
        j = ((r.get("data") or {}).get("payload") or {}).get("j")
        if j:
            latest_j[r.get("match_id")] = j
    j_buckets = {}
    for mid, j in latest_j.items():
        models = (j.get("runner") or {}).get("models") or []
        j_buckets.setdefault("+".join(models) or "unrecorded", []).append(mid)

    latest_jd = {}
    for r in judgments:
        d = r.get("data") or {}
        if d.get("match_id"):
            latest_jd[d["match_id"]] = d
    jd_buckets, same = {}, []
    for mid, d in latest_jd.items():
        jd_buckets.setdefault(d.get("model") or "unrecorded", []).append(mid)
        if d.get("model") and d["model"] in (d.get("reviewer_models") or []):
            same.append(mid)

    def real(buckets):
        return [b for b in buckets if b not in ("unrecorded", "undeclared")]
    return {
        "J": {b: sorted(v) for b, v in sorted(j_buckets.items())},
        "judgment": {b: sorted(v) for b, v in sorted(jd_buckets.items())},
        "mixed": {"J": len(real(j_buckets)) > 1,
                  "judgment": len(real(jd_buckets)) > 1},
        "reviewer_same_as_producer": sorted(same),
    }


def model_attribution(config):
    return _attribute_models(store_io.read("locks", era=config.era),
                             store_io.read("judgments", era=config.era))


def _competition(competition_id):
    from football_ai.competitions import ACTIVE
    for comp in ACTIVE:
        if comp["config"].competition_id == competition_id:
            return comp
    known = [c["config"].competition_id for c in ACTIVE]
    raise KeyError(f"unknown competition {competition_id!r}; ACTIVE has {known}")


def _selfcheck_fetch_due():
    """The quota gate, on synthetic records (pure: no store, no network)."""
    fails = []
    utc = timezone.utc

    def lock(mid, k=None, version=1, at="2026-10-09T10:00:00+00:00"):
        payload = {"match_id": mid}
        if k is not None:
            payload["j"] = {"probs_90": {}, "runner": {"k_valid": k}}
        return {"match_id": mid, "version": version,
                "data": {"locked_at": at, "payload": payload}}

    def odd(mid, at, market="1x2", closing=False):
        return {"match_id": mid, "data": {"match_id": mid, "market": market,
                                          "closing": closing, "fetched_at": at}}

    now = datetime(2026, 10, 11, 12, 35, tzinfo=utc)
    fx = [{"match_id": "c", "kickoff_utc": "2026-10-11T13:00:00+00:00"},   # T-25min
          {"match_id": "u", "kickoff_utc": "2026-10-11T13:00:00+00:00"},   # never locked
          {"match_id": "e", "kickoff_utc": "2026-10-12T19:00:00+00:00"},   # T-30.4h
          {"match_id": "deg", "kickoff_utc": "2026-10-12T19:00:00+00:00"},  # T-30.4h
          {"match_id": "far", "kickoff_utc": "2026-10-16T19:00:00+00:00"},  # T-126h
          {"match_id": "gone", "kickoff_utc": "2026-10-11T12:00:00+00:00"},  # kicked off
          {"match_id": "gone2", "kickoff_utc": "2026-10-11T12:30:00+00:00"}]  # kicked off

    def due(locks, odds, at=now):
        return fetch_due(fx, locks, odds, at)

    # (a) closing due: a locked fixture (M only is enough) 10-45 min out
    d = due([lock("c")], [])
    if not d["call"] or d["closing_due"] != ["c"] or d["entry_due"]:
        fails.append(f"a locked fixture 25 min from kickoff with no closing "
                     f"price is closing-due: {d}")
    for mins, want in ((10, True), (45, True), (9, False), (46, False)):
        at = datetime(2026, 10, 11, 13, 0, tzinfo=utc) - timedelta(minutes=mins)
        if bool(due([lock("c")], [], at)["closing_due"]) != want:
            fails.append(f"closing-due at T-{mins}min should be {want} "
                         f"(the window is [now+10, now+45], inclusive)")

    # (b) entry due: M and a usable J, within 100h, no snapshot of any kind
    d = due([lock("e", k=3)], [])
    if not d["call"] or d["entry_due"] != ["e"] or d["closing_due"]:
        fails.append(f"a J-locked fixture within 100h with no price is "
                     f"entry-due: {d}")
    if due([lock("e", k=None)], [])["entry_due"]:
        fails.append("an M-only fixture is never entry-due (pricing it would "
                     "foreclose its J)")
    # priced 20 min after its J lock: too soon to count as a failed attempt,
    # so only "it already has a price" can stop the call here
    if due([lock("e", k=3, at="2026-10-11T09:00:00+00:00")],
           [odd("e", "2026-10-11T09:20:00+00:00", market="totals")])["call"]:
        fails.append("a fixture with any snapshot already (here a totals row) "
                     "is never entry-due: one entry price, not one per tick")

    # neither: nothing near kickoff, every usable J already priced, a J
    # beyond the horizon, a degraded J, a kicked-off fixture -> no call
    quiet = [lock("c"), lock("e", k=3), lock("far", k=3), lock("deg", k=1)]
    d = due(quiet, [odd("e", "2026-10-10T11:00:00+00:00", market="totals")],
            datetime(2026, 10, 11, 11, 0, tzinfo=utc))
    if d["call"] or d["closing_due"] or d["entry_due"]:
        fails.append(f"nothing due must mean no call (c is 2h out, e already "
                     f"priced, far beyond 100h, deg's J has one sample): {d}")
    if due([lock("gone", k=3), lock("gone2")], [])["call"]:
        fails.append("a fixture past kickoff is never due")
    if due([lock("e", k=3), lock("e", k=1, version=2)], [])["entry_due"]:
        fails.append("the LATEST J decides: a re-run that degraded to k_valid=1 "
                     "is not a usable J, so it is not entry-due")

    # a recent closing price is not fetched again; a stale one, or a closing
    # price for another market only, is
    fresh = odd("c", "2026-10-11T12:22:00+00:00", closing=True)
    if due([lock("c")], [fresh])["call"]:
        fails.append("a closing 1x2 fetched at T-38min is fresh: no second call")
    if due([lock("c")], [odd("c", "2026-10-11T12:00:00+00:00", closing=True)])["call"]:
        fails.append("a closing 1x2 fetched exactly at T-60min still counts as fresh")
    stale = odd("c", "2026-10-11T11:50:00+00:00", closing=True)
    if due([lock("c")], [stale])["closing_due"] != ["c"]:
        fails.append("a closing price from T-70min is not the close: call again")
    other = odd("c", "2026-10-11T12:22:00+00:00", market="totals", closing=True)
    if due([lock("c")], [other])["closing_due"] != ["c"]:
        fails.append("only a 1x2 closing price satisfies the closing gate")
    if due([lock("c")], [odd("c", "not-a-time", closing=True)])["closing_due"] != ["c"]:
        fails.append("a closing row with no readable fetched_at cannot prove "
                     "freshness: still due")

    # an unlocked fixture never triggers a call, however close to kickoff
    if due([], [])["call"] or due([lock("e", k=3)], [odd("e", "2026-10-10T11:00:00+00:00")])["call"]:
        fails.append("an unlocked fixture (u, T-25min) must not trigger a call")

    # an entry already attempted after the J reached main is not retried
    j_at = "2026-10-11T09:00:00+00:00"
    tried = due([lock("e", k=3, at=j_at)], [odd("z", "2026-10-11T10:30:00+00:00")])
    if tried["call"] or tried["entry_tried"] != ["e"]:
        fails.append(f"a call 90 min after the J lock that did not price it "
                     f"means it is not in the response: no retry each tick: {tried}")
    racing = due([lock("e", k=3, at=j_at)], [odd("z", "2026-10-11T09:30:00+00:00")])
    if racing["entry_due"] != ["e"]:
        fails.append("a call 30 min after the lock may predate its push: the "
                     "entry is still due")
    return fails


def _selfcheck_body():
    import tempfile
    from pathlib import Path

    from football_ai.competitions.epl_2026_27 import CONFIG

    fails = []

    if _parse_iso("2026-08-29T16:30:00Z").tzinfo is None:
        fails.append("_parse_iso must return tz-aware datetimes for Z stamps")

    # --- who produced what (2026-10-06) -------------------------------------
    def _lk(mid, v, models=None, has_j=True):
        j = {"runner": ({"models": models} if models is not None else {})}
        return {"match_id": mid, "version": v,
                "data": {"payload": ({"j": j} if has_j else {})}}
    att = _attribute_models(
        [_lk("a", 1, has_j=False), _lk("a", 2, ["m-1"]),     # latest J wins
         _lk("b", 1, ["m-1"]), _lk("c", 1),                  # c: legacy J
         _lk("d", 1, ["m-1", "m-2"])],                       # one J, two models
        [{"data": {"match_id": "a", "model": "m-1",
                   "reviewer_models": ["m-1", "m-9"]}},
         {"data": {"match_id": "b", "model": "undeclared"}},
         {"data": {"match_id": "c"}},                        # legacy row
         {"data": {"match_id": "c", "model": "m-2"}}])       # later row wins
    if att["J"] != {"m-1": ["a", "b"], "m-1+m-2": ["d"], "unrecorded": ["c"]}:
        fails.append(f"J attribution wrong: {att['J']}")
    if att["judgment"] != {"m-1": ["a"], "undeclared": ["b"], "m-2": ["c"]}:
        fails.append(f"judgment attribution wrong: {att['judgment']}")
    if att["mixed"] != {"J": True, "judgment": True}:
        fails.append(f"two real models in a layer must read as MIXED, "
                     f"unrecorded/undeclared must not count: {att['mixed']}")
    if att["reviewer_same_as_producer"] != ["a"]:
        fails.append("a judgment reviewed by its own model must be listed")
    clean = _attribute_models([_lk("a", 1, ["m-1"]), _lk("b", 1)],
                              [{"data": {"match_id": "a", "model": "m-1"}}])
    if clean["mixed"] != {"J": False, "judgment": False}:
        fails.append("one real model plus unrecorded rows is not MIXED")

    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            ko = "2026-08-29T16:30:00+00:00"
            payload = {"match_id": "t_a_b", "teams": {"home": "Arsenal",
                                                      "away": "Chelsea"},
                       "kickoff_utc": ko,
                       "probs_90": {"home": .5, "draw": .3, "away": .2}}
            store_io.append("locks", {"payload": payload, "version": 1},
                            era=CONFIG.era, match_id="t_a_b", version=1)

            before = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
            after = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
            if _locked_unsettled(CONFIG, now=before):
                fails.append("a fixture that has not kicked off is not "
                             "settle-pending")
            if len(_locked_unsettled(CONFIG, now=after)) != 1:
                fails.append("a kicked-off unsettled lock must be pending")

            store_io.append("results", {"match_id": "t_a_b", "score90": [2, 1]},
                            era=CONFIG.era, match_id="t_a_b")
            if _locked_unsettled(CONFIG, now=after):
                fails.append("a settled fixture must drop out of pending")

            # review: scores every recorded reading against the real result
            store_io.append("odds", {"match_id": "t_a_b", "book": "stub",
                                     "market": "1x2", "line": None,
                                     "outcomes": ["home", "draw", "away"],
                                     "odds": [2.0, 3.5, 4.0], "closing": True,
                                     "fetched_at": "2026-08-29T16:20:00+00:00"},
                            era=CONFIG.era, match_id="t_a_b")
            out = review(CONFIG, "t_a_b")
            if out["outcome"] != "home":
                fails.append(f"2-1 must score as a home win, got {out['outcome']}")
            if "M" not in out["readings"]:
                fails.append("review must always score M")
            if not any(k.startswith("market") for k in out["readings"]):
                fails.append("review must score the market snapshot when one exists")
            if not out["closing_snapshot_available"]:
                fails.append("a closing-stamped snapshot must be reported as available")
            if out["readings"]["M"]["rps"] <= 0:
                fails.append("a non-certain forecast must have positive RPS")
            if out["judgment_lean"] is not None:
                fails.append("no judgments row means no lean, not an empty one")

            # the judgment layer's COMMITTED LEAN must come back out at
            # settlement -- a mandatory deliverable (charter section 4) that is
            # never read back cannot be scored, which is the only way to
            # find out whether the layer is worth its cost
            store_io.append("judgments", {
                "match_id": "t_a_b",
                "judgment_probs_90": {"home": 0.5, "draw": 0.25, "away": 0.25},
                "direction": "home", "prob": 0.5, "confidence": "medium",
                "j_status": "absent", "diverges_from_j": False,
                "divergence_note": "no J on this fixture",
                "capabilities_used": [{"capability": "project_code_arithmetic",
                                       "what": "recomputed with report.lambdas_at"}],
                "basis": "test", "lambda": 0.2, "f_star": 0.1,
                "lambda_components": {"mech": 0.2, "bias": 1.0,
                                      "precedent": 1.0, "tail": 1.0},
                "stake_frac": 0.25 * 0.2 * 0.1,
            }, era=CONFIG.era, match_id="t_a_b")
            out2 = review(CONFIG, "t_a_b")
            if "judgment" not in out2["readings"]:
                fails.append("review must score the judgment distribution")
            lean = out2["judgment_lean"]
            if not lean or lean["direction"] != "home" or lean["confidence"] != "medium":
                fails.append(f"review must surface the committed lean: {lean}")
            if lean["hit"] is not True:
                fails.append("a home lean on a 2-1 home win must read as a hit")
            if lean["schema_errors"]:
                fails.append(f"a schema-valid lean must report no errors: "
                             f"{lean['schema_errors']}")
            if abs(lean["lambda"] * lean["f_star"] - 0.02) > 1e-12:
                fails.append("review must carry lambda and f_star through so "
                             "the lambda*f_star ranking can be rebuilt later")

            # --- CLV, the charter's PRIMARY yardstick ---------------------
            # With only a closing snapshot on file there is no entry price,
            # and the honest output is an absence with a stated reason --
            # never a 0.0 that would enter the ledger as a real measurement.
            if out2["clv"] is not None or not out2["clv_absent_because"]:
                fails.append(f"with no entry price, CLV must be absent WITH a "
                             f"reason, got {out2['clv']} / "
                             f"{out2['clv_absent_because']}")

            # add a genuine pre-close entry: now CLV is computable
            store_io.append("odds", {"match_id": "t_a_b", "book": "stub",
                                     "market": "1x2", "line": None,
                                     "outcomes": ["home", "draw", "away"],
                                     "odds": [2.5, 3.4, 3.0], "closing": False,
                                     "fetched_at": "2026-08-27T10:00:00+00:00"},
                            era=CONFIG.era, match_id="t_a_b")
            out3 = review(CONFIG, "t_a_b")
            if out3["clv"] is None:
                fails.append(f"with an entry and a close on file CLV must be "
                             f"computed: {out3['clv_absent_because']}")
            else:
                per = out3["clv"]["per_layer"]
                if "M" not in per or "judgment" not in per:
                    fails.append(f"CLV must cover every instrument layer: "
                                 f"{sorted(per)}")
                if any(k.startswith("market(") for k in per):
                    fails.append("the market reading is the benchmark — "
                                 "scoring it against itself is meaningless")
                for name, row in per.items():
                    if row["clv"] is None or row["entry_odds"] is None:
                        fails.append(f"{name}: CLV needs a real quote, got {row}")
                # the differential is the question being asked: does the
                # layer add anything over the one below it?
                got = out3["clv_differentials"].get("judgment_minus_M")
                want = per["judgment"]["clv"] - per["M"]["clv"]
                if got is None or abs(got - want) > 1e-12:
                    fails.append(f"judgment_minus_M differential wrong: "
                                 f"{got} vs {want}")
                if out3["clv_differentials"].get("J_minus_M") is not None:
                    fails.append("with no J on this fixture the J differential "
                                 "must be None, not silently zero")

            # --- materialisation checks are READ at settlement ------------
            if out3["materialisation"] != []:
                fails.append("a fixture with no J has no factors to judge")
            store_io.append("locks", {"payload": dict(payload, j={
                "probs_90": {"home": 0.5, "draw": 0.25, "away": 0.25},
                "factors": [{"mechanism_type": "rest_travel",
                             "direction": "home", "lever": "d_eff",
                             "materialisation_check": "home side's high "
                             "regains in the first 30 minutes"}]}),
                "version": 2}, era=CONFIG.era, match_id="t_a_b", version=2)
            out4 = review(CONFIG, "t_a_b")
            if len(out4["materialisation"]) != 1:
                fails.append(f"every J factor's materialisation_check must "
                             f"surface at settlement: {out4['materialisation']}")
            elif not out4["materialisation"][0]["check"]:
                fails.append("the check text itself must be carried through — "
                             "deciding after the match what counts as "
                             "materialisation is hindsight, not a check")
            elif out4["materialisation"][0]["verdict"] is not None:
                fails.append("review must not pre-fill the verdict; a human "
                             "judges it and writes it back")

            try:
                review(CONFIG, "t_never_played")
                fails.append("review of an unsettled match must raise")
            except ValueError:
                pass

            # --- run_j: the guards that used to live in a runbook ---------
            import json as _json
            good_claim = {
                "claim": "Left-back ruled out, hamstring, club statement",
                "category": "injury", "source": "club site", "source_tier": "1a",
                "access_time": "2026-09-03T09:00:00+00:00",
                "absorption": "likely_priced",
                "absorption_reason": "official statement, widely carried",
                "dimension": "availability"}
            future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
            # M must be internally consistent with the goal curve, or J's
            # arithmetic gate correctly rejects every sample -- the fixture
            # under test here is run_j's plumbing, not a planted mismatch
            import math as _math
            from football_ai.models.m_dc import outcome_probs as _op
            _ga, _gb, _rho = 0.2954, 0.1644, -0.0372
            _d = 60.0
            _lh = _math.exp(_ga + _gb * _d / 100.0)
            _la = _math.exp(_ga - _gb * _d / 100.0)
            _w, _dr, _l = _op(_lh, _la, _rho)
            _mp = {"home": _w, "draw": _dr, "away": _l}
            base = {"match_id": "t_j", "teams": {"home": "H", "away": "A"},
                    "kickoff_utc": future, "probs_90": dict(_mp),
                    "lambdas": {"home": _lh, "away": _la},
                    "inputs": {"elo_diff_effective": _d}}
            store_io.append("locks", {"payload": base, "version": 1},
                            era=CONFIG.era, match_id="t_j", version=1)

            def _stub_llm(_prompt):
                return _json.dumps({
                    "probs_90": dict(_mp),
                    "factors": [], "basis": ["nothing cleared the bar"],
                    "invalidation": [], "self_flags": []})

            amended = []

            def _amend(mid, payload, reason):
                amended.append((mid, payload, reason))
                return {"version": 2}

            curve = lambda: (0.2954, 0.1644, -0.0372)

            # a lock predating inputs.goal_curve must have it FILLED IN, under
            # the key names the arithmetic gate reads -- getting this wrong by
            # hand is what silently disabled the gate on the one live J
            out = run_j(CONFIG, "t_j", [good_claim], _amend, goal_curve=curve,
                        llm=_stub_llm, k=1)
            if not out["goal_curve_added"]:
                fails.append("run_j must add a missing goal_curve")
            if not amended:
                fails.append("run_j must amend the lock with the J payload")
            else:
                gc = amended[0][1]["inputs"]["goal_curve"]
                if set(gc) != {"g_a", "g_b", "dixon_coles_rho"}:
                    fails.append(f"goal_curve must use j_runner's key names "
                                 f"(dixon_coles_rho, not rho): {sorted(gc)}")
                if not amended[0][1].get("j"):
                    fails.append("the amended payload must carry the J block")
                # a stub that names no model is recorded as not reported,
                # in the block, the summary and the amend reason alike
                if out["models"] != [] or "model not reported" not in amended[0][2] \
                        or amended[0][1]["j"]["runner"]["models"] != []:
                    fails.append("an unreported model must be visible in the "
                                 "J block, the summary and the amend reason")

            # an amend that is refused must not lose the J that took ten minutes
            import contextlib as _cl
            import io as _sio

            def _refusing(mid, payload, reason):
                raise ValueError("REFUSED: the checkout is behind origin")
            err_ = _sio.StringIO()
            try:
                with _cl.redirect_stderr(err_):
                    run_j(CONFIG, "t_j", [good_claim], _refusing,
                          goal_curve=curve, llm=_stub_llm, k=1)
                fails.append("a refused amend must propagate")
            except ValueError:
                pass
            m_ = re.search(r"preserved at (\S+)", err_.getvalue())
            if not m_ or not Path(m_.group(1)).exists() \
                    or "probs_90" not in Path(m_.group(1)).read_text(encoding="utf-8"):
                fails.append(f"the J result must be preserved when amend refuses: "
                             f"{err_.getvalue()!r}")
            elif m_:
                Path(m_.group(1)).unlink()

            # a callable that names its model, plus an explicit --model
            # request: both land on the record, and neither is restricted
            from football_ai.research.j_runner import _llm_text as _lt
            amended2 = []

            def _amend2(mid, payload, reason):
                amended2.append((mid, payload, reason))
                return {"version": 2}
            out2 = run_j(CONFIG, "t_j", [good_claim], _amend2,
                         goal_curve=curve, k=1, j_model="m-y",
                         llm=lambda _p: _lt(_stub_llm(_p), ["m-x"],
                                            {"m-x": {"inputTokens": 1,
                                                     "outputTokens": 1}}))
            runner2 = (amended2[0][1]["j"]["runner"] if amended2 else {})
            if out2["models"] != ["m-x"] or runner2.get("models") != ["m-x"] \
                    or runner2.get("model_requested") != "m-y" \
                    or "m-x" not in amended2[0][2]:
                fails.append(f"run_j must record the model that answered and "
                             f"the one requested: {out2.get('models')} "
                             f"{runner2.get('model_requested')}")

            # the bundle J read must land in the store, once, and a dry run
            # must write nothing
            ev = [r for r in store_io.read("evidence", era=CONFIG.era)
                  if r.get("match_id") == "t_j"]
            if len(ev) != 1 or ev[0]["data"].get("claims") != [good_claim]                     or not out.get("evidence_stored"):
                fails.append("run_j must persist the validated evidence bundle "
                             "it used, exactly once")
            again = run_j(CONFIG, "t_j", [good_claim], _amend,
                          goal_curve=curve, llm=_stub_llm, k=1)
            other = run_j(CONFIG, "t_j", [dict(good_claim, claim="A second "
                          "left-back doubt, training report")], _amend,
                          goal_curve=curve, llm=_stub_llm, k=1, dry_run=True)
            n_ev = len([r for r in store_io.read("evidence", era=CONFIG.era)
                        if r.get("match_id") == "t_j"])
            if again.get("evidence_stored") or other.get("evidence_stored")                     or n_ev != 1:
                fails.append("an identical bundle must not be stored twice, "
                             "and a dry run must store nothing")

            # no provider and no curve on the lock: refuse rather than run
            # with the arithmetic gate quietly off
            store_io.append("locks", {"payload": dict(base, match_id="t_j2"),
                                      "version": 1},
                            era=CONFIG.era, match_id="t_j2", version=1)
            try:
                run_j(CONFIG, "t_j2", [good_claim], _amend, llm=_stub_llm, k=1)
                fails.append("run_j must refuse when it cannot fill the goal "
                             "curve — an unchecked J is worse than no J")
            except ValueError:
                pass

            # a bundle carrying a market number never reaches the model
            leaky = dict(good_claim, claim="Kalshi has the home side at 62%")
            try:
                run_j(CONFIG, "t_j2", [leaky], _amend, goal_curve=curve,
                      llm=_stub_llm, k=1)
                fails.append("run_j must refuse a bundle with a market leak")
            except ValueError as e:
                if "market-derived" not in str(e):
                    fails.append(f"the refusal should name the leak: {e}")
                # and it must not be the channel through which the analyst
                # reads the flagged number (it used to print it verbatim)
                if "62" in str(e) or "Kalshi" in str(e):
                    fails.append(f"run_j's refusal echoed the flagged text: {e}")

            # a priced fixture is J-FORECLOSED, and must say so
            store_io.append("odds", {"match_id": "t_j2", "book": "stub",
                                     "market": "1x2"},
                            era=CONFIG.era, match_id="t_j2")
            try:
                run_j(CONFIG, "t_j2", [good_claim], _amend, goal_curve=curve,
                      llm=_stub_llm, k=1)
                fails.append("run_j must refuse a fixture that already has a price")
            except ValueError as e:
                if "FORECLOSED" not in str(e):
                    fails.append(f"the refusal should name foreclosure, not "
                                 f"read as a generic amend failure: {e}")

            # re-running over a SOUND existing J would overwrite it
            store_io.append("locks", {"payload": dict(
                base, match_id="t_j3",
                j={"probs_90": {}, "runner": {"k_valid": 3, "k_requested": 3}}),
                "version": 1}, era=CONFIG.era, match_id="t_j3", version=1)
            try:
                run_j(CONFIG, "t_j3", [good_claim], _amend, goal_curve=curve,
                      llm=_stub_llm, k=1)
                fails.append("run_j must refuse to overwrite a sound J")
            except ValueError:
                pass

            # ...unless the bundle J saw has since been materially upgraded:
            # once, before any price (owner, 2026-10-10)
            thin = [good_claim]
            rich = [dict(good_claim, claim=f"Starter {i} status confirmed by club, {i}")
                    for i in range(4)]
            store_io.append("evidence", {"match_id": "t_j6", "claims": thin},
                            era=CONFIG.era, match_id="t_j6")
            store_io.append("locks", {"payload": dict(
                dict(base, probs_90=dict(_mp), lambdas={"home": _lh, "away": _la}),
                match_id="t_j6",
                j={"probs_90": dict(_mp), "runner": {"k_valid": 3, "k_requested": 3}}),
                "version": 1}, era=CONFIG.era, match_id="t_j6", version=1)
            try:
                run_j(CONFIG, "t_j6", [good_claim, dict(good_claim, claim="x y")],
                      _amend, goal_curve=curve, llm=_stub_llm, k=1)
                fails.append("a sound J must not be re-run on a bundle that is "
                             "not a material upgrade (1 -> 2 tier-1a claims)")
            except ValueError:
                pass
            before = len(amended)
            run_j(CONFIG, "t_j6", rich, _amend, goal_curve=curve, llm=_stub_llm, k=1)
            if len(amended) != before + 1 or "evidence-upgrade" not in amended[-1][2]:
                fails.append("a sound J on an unpriced fixture must be re-runnable "
                             "once after a material evidence upgrade, and say so")
            # the amend in this selfcheck does not append a lock; record the
            # second sound J so the cap is visible to the next attempt
            store_io.append("locks", {"payload": dict(
                dict(base, probs_90=dict(_mp), lambdas={"home": _lh, "away": _la}),
                match_id="t_j6",
                j={"probs_90": dict(_mp), "runner": {"k_valid": 3, "k_requested": 3}}),
                "version": 2}, era=CONFIG.era, match_id="t_j6", version=2)
            try:
                run_j(CONFIG, "t_j6", rich + rich, _amend, goal_curve=curve,
                      llm=_stub_llm, k=1)
                fails.append("an evidence-upgrade re-run is allowed once only")
            except ValueError:
                pass

            # ...but a DEGRADED J on an UNPRICED fixture may be re-run: the
            # samples were lost to the environment, not to judgment, and no
            # price exists for anyone to have been influenced by
            store_io.append("locks", {"payload": dict(
                dict(base, probs_90=dict(_mp), lambdas={"home": _lh, "away": _la}),
                match_id="t_j4",
                j={"probs_90": dict(_mp),
                   "runner": {"k_valid": 1, "k_requested": 3}}),
                "version": 1}, era=CONFIG.era, match_id="t_j4", version=1)
            before = len(amended)
            run_j(CONFIG, "t_j4", [good_claim], _amend, goal_curve=curve,
                  llm=_stub_llm, k=1)
            if len(amended) != before + 1:
                fails.append("a degraded J on an unpriced fixture must be "
                             "re-runnable -- the samples were lost to the "
                             "environment, and nothing is contaminated")

            # once a price exists, the same degraded J is FROZEN: whoever
            # rewrote it would know which direction is interesting
            store_io.append("odds", {"match_id": "t_j4", "book": "stub",
                                     "market": "1x2"},
                            era=CONFIG.era, match_id="t_j4")
            try:
                run_j(CONFIG, "t_j4", [good_claim], _amend, goal_curve=curve,
                      llm=_stub_llm, k=1)
                fails.append("a priced fixture's J must be frozen even when "
                             "degraded -- re-running after seeing the price "
                             "is rolling until satisfied")
            except ValueError as e:
                if "FORECLOSED" not in str(e):
                    fails.append(f"the refusal should name foreclosure: {e}")
        finally:
            store_io.STORE_DIR = old

    # an un-J'd fixture must NOT be priced early, but MUST be priced at
    # the close (CLV needs it, and by then J is moot)
    old_store2 = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td2:
        store_io.STORE_DIR = Path(td2)
        try:
            ko2 = "2026-09-05T14:00:00+00:00"
            store_io.append("locks", {"version": 1, "payload": {
                "match_id": "t_noj", "kickoff_utc": ko2,
                "probs_90": {"home": .4, "draw": .3, "away": .3}}},
                era=CONFIG.era, match_id="t_noj", version=1)
            store_io.append("locks", {"version": 1, "payload": {
                "match_id": "t_hasj", "kickoff_utc": ko2, "j": {"probs_90": {}},
                "probs_90": {"home": .4, "draw": .3, "away": .3}}},
                era=CONFIG.era, match_id="t_hasj", version=1)
            fx2 = [{"match_id": "t_noj", "kickoff_utc": ko2},
                   {"match_id": "t_hasj", "kickoff_utc": ko2}]
            evs = [{"id": "e", "sport_key": "s", "commence_time": ko2,
                    "home_team": h, "away_team": a,
                    "bookmakers": [{"key": "bk", "last_update": ko2, "markets": [
                        {"key": "h2h", "outcomes": [
                            {"name": h, "price": 2.0}, {"name": a, "price": 3.0},
                            {"name": "Draw", "price": 3.5}]}]}]}
                   for h, a in (("H1", "A1"), ("H2", "A2"))]
            import football_ai.data.the_odds_api as _api
            real_to = _api.to_snapshots
            real_fetch = _api.fetch_odds
            _api.fetch_odds = lambda *a, **k: (evs, {}, Path(td2) / "raw.json")
            _api.to_snapshots = lambda *a, **k: (
                [{"match_id": m, "book": "bk", "market": "1x2", "line": None,
                  "outcomes": ["home", "draw", "away"], "odds": [2.0, 3.5, 3.0],
                  "fetched_at": "x", "closing": False}
                 for m in ("t_noj", "t_hasj")], [])
            try:
                early = snapshot(CONFIG, fx2, "s", key="k",
                                 now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
                if early["deferred_no_j"] != ["t_noj"]:
                    fails.append(f"an un-J'd fixture must not be priced early, "
                                 f"got deferred={early['deferred_no_j']}")
                if early["fixtures"] != ["t_hasj"]:
                    fails.append(f"the J'd fixture should still be priced, "
                                 f"got {early['fixtures']}")
                close = snapshot(CONFIG, fx2, "s", key="k",
                                 now=datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc))
                if "t_noj" not in close["closing_fixtures"]:
                    fails.append("at the close an un-J'd fixture MUST be "
                                 "priced — CLV needs it and J is moot by then")

                # A J with a single valid sample cannot report dispersion,
                # so it is not a finished J: pricing it would freeze the
                # degraded reading permanently (epl2627_gw03_new_bou,
                # 2026-09-04). It must defer exactly like no J at all.
                for rec in store_io.read("locks", era=CONFIG.era):
                    if rec.get("match_id") == "t_hasj":
                        rec["data"]["payload"]["j"]["runner"] = {"k_valid": 1,
                                                                "k_requested": 3}
                        store_io.append("locks", rec["data"], era=CONFIG.era,
                                        match_id="t_hasj", version=2)
                        break
                thin = snapshot(CONFIG, fx2, "s", key="k",
                                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
                if "t_hasj" not in thin["deferred_no_j"]:
                    fails.append(
                        f"a J with k_valid=1 has no dispersion and is not a "
                        f"finished J — pricing it freezes that permanently; "
                        f"deferred={thin['deferred_no_j']}")

                # --when-due: no call at all with nothing due; with a
                # closing price due, one call, and a due fixture the
                # response lacks is named (main fails the run on it)
                calls = []
                _api.fetch_odds = lambda *a, **k: (calls.append(1) or
                                                   (evs, {}, Path(td2) / "raw.json"))
                quiet = snapshot(CONFIG, fx2, "s", key="k", when_due=True,
                                 now=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc))
                if calls or not quiet.get("skipped"):
                    fails.append("with nothing due (kickoff 2h out, the only J "
                                 "degraded) --when-due must not call the API")
                due_run = snapshot(CONFIG, fx2, "s", key="k", when_due=True,
                                   now=datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc))
                if len(calls) != 1 or due_run["due"]["closing_due"] != ["t_hasj", "t_noj"]:
                    fails.append(f"two fixtures 30 min from kickoff with no readable "
                                 f"closing price: one call, both due; calls={len(calls)} "
                                 f"due={due_run.get('due')}")
                elif due_run["due_not_priced"]:
                    fails.append(f"both were in the response and stamped closing: "
                                 f"{due_run['due_not_priced']}")
                _api.to_snapshots = lambda *a, **k: (
                    [{"match_id": "t_hasj", "book": "bk", "market": "1x2",
                      "line": None, "outcomes": ["home", "draw", "away"],
                      "odds": [2.0, 3.5, 3.0], "fetched_at": "x",
                      "closing": False}], [])
                missing = snapshot(CONFIG, fx2, "s", key="k", when_due=True,
                                   now=datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc))
                if missing["due_not_priced"] != ["t_noj"]:
                    fails.append(f"a due fixture missing from the response must be "
                                 f"named: {missing['due_not_priced']}")
            finally:
                _api.to_snapshots = real_to
                _api.fetch_odds = real_fetch
        finally:
            store_io.STORE_DIR = old_store2

    # the due guard must return without touching the network. The fetch is
    # stubbed so that a broken guard fails this check instead of sending a
    # real request (it did, with this dummy key, before 2026-10-10).
    from football_ai.competitions.epl_2026_27 import CONFIG as _C
    import football_ai.data.the_odds_api as _api2
    far = [{"match_id": "x", "kickoff_utc": "2027-05-01T14:00:00+00:00"}]
    reached, real_fetch2 = [], _api2.fetch_odds

    def _no_network(*a, **k):
        reached.append(1)
        raise RuntimeError("the due guard let a call through (test)")
    _api2.fetch_odds = _no_network
    try:
        noop = snapshot(_C, far, "soccer_epl", key="SHOULD-NEVER-BE-USED",
                        now=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
                        when_due=True)
    except RuntimeError:
        noop = {}
    finally:
        _api2.fetch_odds = real_fetch2
    if reached or not noop.get("skipped") or noop["stored"] != 0:
        fails.append("the due guard must no-op before any API call when "
                     "nothing is due")

    fails.extend(_selfcheck_fetch_due())

    # the window filter is what stops a routine closing grab from
    # silently pricing (and therefore foreclosing J on) distant fixtures
    from football_ai.competitions.ops import _parse_iso as _p
    now_t = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    fx = [{"match_id": "soon", "kickoff_utc": "2026-08-29T16:30:00+00:00"},
          {"match_id": "far", "kickoff_utc": "2026-09-05T14:00:00+00:00"}]
    kept = [f for f in fx
            if (_p(f.get("kickoff_utc")) or now_t) <= now_t + timedelta(hours=48)]
    if [f["match_id"] for f in kept] != ["soon"]:
        fails.append("only_within_hours must drop fixtures beyond the horizon")

    try:
        _competition("no-such-competition")
        fails.append("_competition must raise on an unknown competition id")
    except KeyError:
        pass

    # the key comes from the gitignored config when it exists, else from the
    # environment (a cloud clone has no such file), else a clear error
    import tempfile
    from pathlib import Path
    saved = os.environ.pop("ODDS_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            try:
                api_key(cfg)
                fails.append("api_key must raise when neither the file nor "
                             "ODDS_API_KEY exists")
            except FileNotFoundError:
                pass
            os.environ["ODDS_API_KEY"] = "env-test-key\r\n"
            if api_key(cfg) != "env-test-key":
                fails.append("api_key must fall back to ODDS_API_KEY when "
                             "the config file is absent, and strip a trailing "
                             "line ending")
            cfg.write_text('{"odds_api_key": "file-test-key"}', encoding="utf-8")
            if api_key(cfg) != "file-test-key":
                fails.append("api_key must prefer the config file when it "
                             "exists, so the owner's machine is unchanged")
    finally:
        os.environ.pop("ODDS_API_KEY", None)
        if saved is not None:
            os.environ["ODDS_API_KEY"] = saved

    return fails


def _selfcheck_materialise():
    """The mechanism-ledger writer and the precedent reader it feeds."""
    import tempfile
    from football_ai.competitions.epl_2026_27 import CONFIG
    from football_ai.decision.sizing import precedent_stats
    fails = []
    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            j = {"probs_90": {}, "factors": [
                {"mechanism_type": "squad_composition_change", "direction": "away",
                 "materialisation_check": "away side's new striker starts and has >=2 shots"},
                {"mechanism_type": "rest_travel", "direction": "home",
                 "materialisation_check": "away side shows lower second-half xG"}]}
            store_io.append("locks", {"version": 1, "payload": {"match_id": "t_m", "j": j}},
                            era=CONFIG.era, match_id="t_m", version=1)
            v = [{"factor_index": 0, "verdict": "yes", "basis": "started, 3 shots (Understat)"}]
            try:
                record_materialisation(CONFIG, "t_m", v, "selfcheck")
                fails.append("a verdict before settlement must be refused")
            except ValueError:
                pass
            store_io.append("results", {"match_id": "t_m", "score90": [0, 1]},
                            era=CONFIG.era, match_id="t_m")
            if materialisation_pending(CONFIG) != [("t_m", [0, 1])]:
                fails.append(f"both factors must be pending: {materialisation_pending(CONFIG)}")
            record_materialisation(CONFIG, "t_m", v, "selfcheck")
            if precedent_stats("squad_composition_change", CONFIG.era) != (1, 1.0):
                fails.append("a written 'yes' must count as one hit")
            for bad in ([{"factor_index": 5, "verdict": "yes", "basis": "x"}],
                        [{"factor_index": 1, "verdict": "maybe", "basis": "x"}],
                        [{"factor_index": 1, "verdict": "no", "basis": " "}],
                        v):
                try:
                    record_materialisation(CONFIG, "t_m", bad, "selfcheck")
                    fails.append(f"must refuse {bad}")
                except ValueError:
                    pass
            record_materialisation(CONFIG, "t_m", [dict(v[0], verdict="no",
                                   basis="correction: he was benched")], "selfcheck",
                                   supersede=True)
            if precedent_stats("squad_composition_change", CONFIG.era) != (1, 0.0):
                fails.append("a superseding verdict must replace, not add to, the old one")
            if materialisation_pending(CONFIG) != [("t_m", [1])]:
                fails.append("only the unjudged factor may remain pending")
        finally:
            store_io.STORE_DIR = old
    return fails


def _selfcheck():
    """Run the module's checks offline, then prove run_j refuses on a stale
    checkout."""
    from football_ai.store import sync as store_sync

    saved = os.environ.get(store_sync.SKIP_ENV)
    os.environ[store_sync.SKIP_ENV] = "1"
    try:
        fails = _selfcheck_body()
    finally:
        if saved is None:
            os.environ.pop(store_sync.SKIP_ENV, None)
        else:
            os.environ[store_sync.SKIP_ENV] = saved

    fails += _selfcheck_materialise()

    # the scheduled job: both steps with the registered flags; one failing
    # must not stop the other, and must surface as a non-zero exit
    global main
    real_main, seen = main, []

    def _fake_main(argv=None):
        seen.append(list(argv))
        if argv[0] == "snapshot":
            raise RuntimeError("boom (test)")

    main = _fake_main
    try:
        import contextlib
        import io as _io
        with contextlib.redirect_stderr(_io.StringIO()):
            rc = _matchday("epl-2026-27")
    finally:
        main = real_main
    if [a[0] for a in seen] != ["snapshot", "settle"]:
        fails.append(f"matchday must run snapshot then settle, ran {seen}")
    elif ("--when-due" not in seen[0] or "100" not in seen[0]
          or "100.0" in seen[0]):
        fails.append(f"matchday must pass --when-due and the registered "
                     f"horizon: {seen[0]}")
    if rc != 1:
        fails.append("a failing step must make matchday exit non-zero, and "
                     "must not skip the next step")

    real = store_sync.assert_current

    def _behind(*a, **k):
        raise store_sync.SyncError("behind origin (test)")

    store_sync.assert_current = _behind
    try:
        called = []
        try:
            run_j(None, "t_sync", [], lambda *a: None,
                  llm=lambda p: called.append(p) or "{}")
            fails.append("run_j must refuse while the checkout is behind origin")
        except store_sync.SyncError:
            pass
        if called:
            fails.append("run_j must not reach the model on a stale checkout")
    finally:
        store_sync.assert_current = real
    return fails


# The unattended scheduled job, defined HERE and nowhere else (the Windows
# .cmd and the cloud workflow both just call `ops matchday`). Safe to run on
# a frequent schedule (every 20 minutes, see schedule.TICK_MINUTES):
#   * snapshot (`--when-due`) makes NO API call at all unless fetch_due finds
#     a closing or an entry price owed, so a tick with nothing due costs
#     nothing;
#   * MATCHDAY_ONLY_WITHIN_HOURS keeps it from pricing fixtures still days
#     away; a fixture with no J is deferred by snapshot() itself except in
#     the closing window;
#   * settle runs every time (it spends no quota): it reads scores from the
#     declared results source and simply finds nothing until it publishes.
# The job never prints a price, so its log is safe to read.
MATCHDAY_ONLY_WITHIN_HOURS = _schedule.ONLY_WITHIN_HOURS


# `snapshot`, `settle`, `matchday` are what the scheduled job runs; the rest are
# for a session (see ops_tools.py and `models`/`review`/`due` above).
OPERATOR_ACTIONS = ("lock", "evidence", "evidence-brief", "run-j",
                    "judgment-brief", "done", "preflight", "summary")
ACTIONS = ["snapshot", "settle", "review", "due", "matchday", "models", "materialise",
           *OPERATOR_ACTIONS]


def _matchday(competition):
    """Run snapshot then settle; a failure in one must not stop the other.
    Returns 0, or 1 if any step raised (so the scheduler shows it failed and
    the owner hears about it, instead of a silent log line)."""
    import traceback
    rc = 0
    steps = (
        ["snapshot", "--when-due", "--only-within-hours",
         str(MATCHDAY_ONLY_WITHIN_HOURS)],
        ["settle"],
    )
    for step in steps:
        try:
            main([*step, "--competition", competition])
        except Exception:  # noqa: BLE001 - report, keep going, fail at the end
            traceback.print_exc()
            rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Unattended ops for a live competition window.")
    ap.add_argument("action", choices=ACTIONS)
    ap.add_argument("--competition", default="epl-2026-27")
    ap.add_argument("--match-id")
    ap.add_argument("--sport-key", default="soccer_epl")
    ap.add_argument("--bookmakers", default="pinnacle")
    ap.add_argument("--closing-window-min", type=int,
                    default=DEFAULT_CLOSING_WINDOW_MIN)
    ap.add_argument("--when-due", action="store_true",
                    help="make the whole run a no-op (and spend no quota) "
                         "unless a closing or entry price is due "
                         "(see fetch_due); what the scheduled job passes")
    ap.add_argument("--only-within-hours", type=float, default=None,
                    help="only store fixtures kicking off within this "
                         "many hours; protects distant fixtures' J option")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--claims", nargs="+",
                    help="path(s) of the evidence bundle (each a JSON list of "
                         "claims; several are merged in order), for "
                         "`evidence` and `run-j`")
    ap.add_argument("--j-model", help="`run-j`: pass --model to the J "
                                      "subprocess (default: the CLI's own)")
    ap.add_argument("--k", type=int, default=3, help="`run-j`: samples")
    ap.add_argument("--side", choices=["home", "away", "match"],
                    help="`evidence-brief`: which scout (home team, away "
                         "team, or the match itself)")
    ap.add_argument("--scout-writes-to",
                    help="`evidence-brief`: absolute path (outside the repo) "
                         "that the scout is told to write its bundle to; "
                         "the prompt itself is printed to stdout")
    ap.add_argument("--store", action="store_true",
                    help="`evidence`: also keep a bundle that passed, in the "
                         "ledger (needs --match-id)")
    ap.add_argument("--gameweek", type=int,
                    help="`done`, `judgment-brief`, `summary`: which matchweek")
    ap.add_argument("--out", help="`summary`: output path (default: reports/)")
    ap.add_argument("--verdicts", help="`materialise`: JSON list of "
                    "{factor_index, verdict, basis}; without it, list what is pending")
    ap.add_argument("--judged-by", help="`materialise`: who judged (model or person)")
    ap.add_argument("--supersede", action="store_true",
                    help="`materialise`: append a correction to an already judged factor")
    ap.add_argument("--no-probe", action="store_true",
                    help="`preflight`: skip the isolated claude -p probe")
    args = ap.parse_args(argv)

    if args.action == "matchday":
        return _matchday(args.competition)

    comp = _competition(args.competition)
    config = comp["config"]
    fixtures = comp.get("upcoming_fixtures") or []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.action in OPERATOR_ACTIONS:
        from football_ai.competitions import ops_tools
        return ops_tools.cli(args, comp)

    if args.action == "snapshot":
        out = snapshot(config, fixtures, args.sport_key,
                       closing_window_min=args.closing_window_min,
                       bookmakers=args.bookmakers,
                       only_within_hours=args.only_within_hours,
                       when_due=args.when_due)
        if out.get("skipped"):
            print(f"[{stamp}] snapshot {config.competition_id}: {out['skipped']}")
            return
        due = out.get("due")
        if due:
            print(f"[{stamp}] snapshot {config.competition_id}: due -- "
                  f"closing: {', '.join(due['closing_due']) or 'none'}; "
                  f"entry: {', '.join(due['entry_due']) or 'none'}")
        print(f"[{stamp}] snapshot {config.competition_id}: "
              f"stored {out['stored']} records for {len(out['fixtures'])} "
              f"fixtures; closing-stamped {len(out['closing_fixtures'])} "
              f"({', '.join(out['closing_fixtures']) or 'none'}); "
              f"quota left {out['quota_remaining']}; raw {out['raw']}")
        if out.get("deferred_no_j"):
            print(f"  not priced yet (J still open, would be foreclosed): "
                  f"{', '.join(out['deferred_no_j'])}")
        if out["excluded_by_window"]:
            print(f"  outside the {args.only_within_hours}h window, not "
                  f"priced: {', '.join(out['excluded_by_window'])}")
        for line in out["refused"]:
            print(f"  REFUSED: {line}")
        if out.get("due_not_priced"):
            # after the stores above, so whatever was written is still pushed;
            # raising fails the scheduled run and GitHub emails the owner
            raise RuntimeError(
                f"due but not priced: {', '.join(out['due_not_priced'])} -- not "
                f"in the API response (club-name mapping, postponement, no "
                f"line yet?); see the REFUSED lines above")
    elif args.action == "settle":
        out = settle_pending(config, dry_run=args.dry_run)
        print(f"[{stamp}] settle {config.competition_id}: "
              f"checked {out['checked']}"
              f"{' (DRY RUN)' if out.get('dry_run') else ''}")
        for line in out["settled"]:
            print(f"  settled: {line}")
        for line in out["pending"]:
            print(f"  pending: {line}")
    elif args.action == "review":
        if not args.match_id:
            ap.error("review needs --match-id")
        print(json.dumps(review(config, args.match_id),
                         ensure_ascii=False, indent=2))
    elif args.action == "materialise":
        if not args.verdicts:
            for mid, idx in materialisation_pending(config):
                _, factors = _j_factors(config, mid)
                print(f"{mid}: {len(idx)} factor(s) pending")
                for i in idx:
                    f = factors[i]
                    print(f"  [{i}] {f.get('mechanism_type')} ({f.get('direction')}): "
                          f"{f.get('materialisation_check')}")
        else:
            if not args.match_id:
                ap.error("materialise --verdicts needs --match-id")
            verdicts = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
            print(json.dumps(record_materialisation(
                config, args.match_id, verdicts, args.judged_by,
                supersede=args.supersede, dry_run=args.dry_run), ensure_ascii=False))
    elif args.action == "models":
        att = model_attribution(config)
        for layer in ("J", "judgment"):
            print(f"{layer}:")
            for bucket, mids in att[layer].items():
                print(f"  {bucket}: {len(mids)}"
                      + ("" if bucket not in ("unrecorded", "undeclared")
                         else f"  ({', '.join(mids)})"))
            if att["mixed"][layer]:
                print(f"  ** MIXED: more than one model produced {layer}; a "
                      f"difference between layers cannot be attributed to "
                      f"the layer unless the comparison is split by model **")
        if att["reviewer_same_as_producer"]:
            print("judgments reviewed by their own model (weaker check): "
                  + ", ".join(att["reviewer_same_as_producer"]))
    else:
        from football_ai.competitions import all_due_items
        for pri, line in all_due_items():
            print(f"[P{pri}] {line}")


if __name__ == "__main__":
    raise SystemExit(main() or 0)
