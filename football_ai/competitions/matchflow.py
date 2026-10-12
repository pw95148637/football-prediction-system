"""Where each fixture is in the flow, and what may legally happen next.

WHY THIS EXISTS (2026-09-02). The per-fixture sequence has eight steps and
the ordering is SAFETY-CRITICAL: price a fixture before its J is locked and
that J is dead permanently, with no way back. Until now the sequence lived
only in a skill's prose, so a session driving nine fixtures had seventy-two
chances to get it wrong from memory. It has already been got wrong once --
epl2627_gw03_ips_liv, priced at T-98.9h by a closing run meant for a
different fixture.

Prose cannot enforce an ordering. This module answers two questions
mechanically: what phase is this fixture in, and what is the next legal
action.

PHASE IS DERIVED, NEVER STORED. It is a function of the append-only store
plus the clock -- locks with or without a `j`, whether odds exist, whether
a result exists, whether kickoff has passed. A stored phase field would be
a second source of truth that can drift from the records it summarises,
and the records are the audit trail.
"""
import argparse
import re
from datetime import datetime, timezone

from football_ai.competitions import schedule
from football_ai.store import io as store_io

DEFAULT_COMPETITION = "epl-2026-27"

# Ordered from earliest to latest. J_FORECLOSED is not a stage of progress
# but a terminal branch: the fixture goes on to be priced and settled, it
# simply can never carry a J. J_DEGRADED is a J the scheduled job will treat
# as absent (fewer than ops.MIN_K_FOR_PRICING valid samples): the fixture
# still owes a J, with the same deadline as an M-only one. JUDGED means the
# judgment record exists; it needs a resolved J (locked or foreclosed), so a
# record written early cannot hide the J a fixture still owes.
PHASES = ("UNLOCKED", "M_LOCKED", "J_DEGRADED", "J_LOCKED", "J_FORECLOSED",
          "PRICED", "JUDGED", "KICKED_OFF", "SETTLED")

# the phases in which a J is still owed, and therefore a deadline applies
OWES_J = ("UNLOCKED", "M_LOCKED", "J_DEGRADED")


def _parse(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _state(config, match_id):
    locks = [r for r in store_io.read("locks", era=config.era)
             if r.get("match_id") == match_id]
    j_blocks = [(r["data"].get("payload") or {}).get("j") for r in locks]
    j_blocks = [j for j in j_blocks if j]
    has_j = bool(j_blocks)
    # a lock with a J but no runner block (written before the runner recorded
    # one) is taken at its word, as ops.snapshot does
    k_valid = (((j_blocks[-1].get("runner") or {}).get("k_valid"))
               if j_blocks else None)
    kickoff = None
    if locks:
        base = min(locks, key=lambda r: r.get("version", 1))
        kickoff = _parse((base["data"].get("payload") or {}).get("kickoff_utc"))
    priced = any(r.get("match_id") == match_id
                 for r in store_io.read("odds", era=config.era))
    settled = any(r["data"].get("match_id") == match_id
                  and "score90" in r["data"]
                  for r in store_io.read("results", era=config.era))
    judged = any(r["data"].get("match_id") == match_id
                 for r in store_io.read("judgments", era=config.era))
    return {"locked": bool(locks), "has_j": has_j, "k_valid": k_valid,
            "priced": priced, "settled": settled, "judged": judged,
            "kickoff": kickoff}


def phase(config, match_id, now=None, kickoff_utc=None):
    """The fixture's phase, derived from the store and the clock."""
    now = now or datetime.now(timezone.utc)
    s = _state(config, match_id)
    ko = s["kickoff"] or _parse(kickoff_utc)

    if s["settled"]:
        return "SETTLED"
    if not s["locked"]:
        return "UNLOCKED"
    if ko is not None and ko < now:
        return "KICKED_OFF"
    from football_ai.competitions.ops import MIN_K_FOR_PRICING
    j_ok = s["has_j"] and (s["k_valid"] is None or s["k_valid"] >= MIN_K_FOR_PRICING)
    if s["judged"] and (j_ok or s["priced"]):
        return "JUDGED"
    if s["has_j"]:
        if s["priced"]:
            return "PRICED"
        return "J_LOCKED" if j_ok else "J_DEGRADED"
    return "J_FORECLOSED" if s["priced"] else "M_LOCKED"


# phase -> what to do next. `{ops}` is the ops command prefix, `{mid}` the
# fixture. A hard-stop action (an interactive `ops snapshot`) is never offered
# as a next step: the scheduled job writes prices.
_NEXT = {
    "UNLOCKED": "{ops} lock --match-id {mid}  (then evidence + run-j in the "
                "SAME session)",
    "M_LOCKED": "evidence, then `{ops} evidence --match-id {mid} --claims "
                "PATH`, then `{ops} run-j --match-id {mid} --claims PATH`, "
                "then push",
    "J_DEGRADED": "re-run `{ops} run-j --match-id {mid} --claims PATH` and "
                  "push (too few valid samples: the job treats this J as absent)",
    "J_LOCKED": "make sure the J is on origin (`python -m football_ai.store.sync "
                "publish -m \"...\"`: the scheduled job reads origin), then wait "
                "for the scheduled price (do NOT run ops snapshot), then report + "
                "judgment + publish",
    "J_FORECLOSED": "M-only for good (a price exists); still owes a committed "
                    "lean (charter section 4)",
    "PRICED": "report (with abstention), judgment, publish; verify with "
              "`{ops} done --gameweek N`",
    "JUDGED": "nothing until kickoff",
    "KICKED_OFF": "settlement is scheduled (by hand only if the source lags: "
                  "`{ops} settle`)",
    "SETTLED": "`{ops} review --match-id {mid}` (a ledger entry, not a verdict)",
}


def ledger_note():
    """A warning line when the ledger holds changes that are not on origin, else
    None. A lock or J that exists only in this checkout does not exist for the
    scheduled job, which reads origin: it will price the fixture without the J.
    `matchflow` derives phases from the local store, so without this line a
    fixture would read "J_LOCKED, wait for the price" while its J is
    unpublished."""
    try:
        from football_ai.competitions import ops_tools
        state, detail = ops_tools._ledger_state()
    except Exception:  # noqa: BLE001 - a check that cannot run says nothing
        return None
    if state in ("dirty", "unpushed"):
        return (f"** ledger {state} ({detail}): publish before anything else -- "
                f"`python -m football_ai.store.sync publish -m \"...\"`. Locks and "
                f"J that are not on origin do not exist for the scheduled job. **")
    return None


def _ops_prefix(config):
    cid = getattr(config, "competition_id", DEFAULT_COMPETITION)
    base = "python -m football_ai.competitions.ops"
    return base if cid == DEFAULT_COMPETITION else f"{base} --competition {cid}"


def next_action(config, match_id, now=None, kickoff_utc=None, date=None):
    """What may legally happen next, plus anything that has been lost.

    `date` (ISO, the UK local match date) is the fallback for a fixture whose
    kickoff time is not confirmed: once that whole date is behind us the
    kickoff has certainly passed, so an unlocked fixture is gone."""
    now = now or datetime.now(timezone.utc)
    ph = phase(config, match_id, now=now, kickoff_utc=kickoff_utc)
    action = _NEXT[ph].format(ops=_ops_prefix(config), mid=match_id)
    lost = None
    if ph == "J_FORECLOSED":
        lost = "J (priced before it was locked) — record as a non-random gap"
    elif ph == "KICKED_OFF":
        s = _state(config, match_id)
        if not s["has_j"]:
            lost = "J (kickoff passed with no J) — M-only permanently"
    elif ph == "UNLOCKED":
        ko = _parse(kickoff_utc)
        if ko is not None:
            passed = ko < now
        else:
            passed = bool(date) and date < now.date().isoformat()
        if passed:
            action = "nothing — kickoff passed with no lock, permanently unlockable"
            lost = "the whole fixture (never locked)"
        elif ko is None:
            action = ("no confirmed kickoff — confirm it against the league's "
                      "fixture pages and fill kickoff_utc in the fixture list; "
                      "lock_match refuses a fixture without one")
    return {"phase": ph, "next": action, "lost": lost}


def matchweek(config, fixtures, gameweek=None, now=None):
    """One row per fixture: phase, next legal action, hours to kickoff."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for f in fixtures:
        if gameweek is not None and f.get("gameweek") != gameweek:
            continue
        ko = _parse(f.get("kickoff_utc"))
        na = next_action(config, f["match_id"], now=now,
                         kickoff_utc=f.get("kickoff_utc"), date=f.get("date"))
        row = {
            "match_id": f["match_id"],
            "date": f.get("date"),
            "fixture": f"{f.get('home_team', '?')} v {f.get('away_team', '?')}",
            "kickoff_utc": ko,
            "hours_to_kickoff": ((ko - now).total_seconds() / 3600
                                 if ko else None),
            "j_deadline_utc": None, "finish_by_utc": None,
            "minutes_to_deadline": None, "at_risk": False,
            **na,
        }
        # a J is owed only until the scheduled job prices the fixture; the
        # deadline is arithmetic on the cron and the closing window
        if row["phase"] in OWES_J and ko is not None and not row["lost"]:
            row["j_deadline_utc"] = schedule.j_deadline(ko)
            row["finish_by_utc"] = schedule.finish_by(ko)
            row["minutes_to_deadline"] = schedule.minutes_left(ko, now)
            row["at_risk"] = not schedule.feasible(ko, now)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["hours_to_kickoff"] is None,
                                       r["hours_to_kickoff"] or 0,
                                       r.get("date") or ""))


def _fmt(dt):
    return dt.strftime("%m-%d %H:%MZ") if dt else "?"


def select(rows, now, days=14, show_all=False):
    """What deserves attention, and counts of what was set aside (never
    silently dropped): settled, lapsed unlocked (gone for good), and unlocked
    fixtures further out than `days`."""
    horizon_date = datetime.fromtimestamp(
        now.timestamp() + days * 86400, timezone.utc).date().isoformat()
    shown, settled, lapsed, later = [], 0, 0, 0
    for r in rows:
        if r["phase"] == "SETTLED":
            settled += 1
            if show_all:
                shown.append(r)
        elif r["phase"] == "UNLOCKED" and r["lost"]:
            lapsed += 1
            if show_all:
                shown.append(r)
        elif (r["phase"] == "UNLOCKED" and not show_all
              and (r["date"] or "") > horizon_date):
            later += 1
        else:
            shown.append(r)
    return shown, {"settled": settled, "lapsed": lapsed, "later": later}


def most_urgent(rows):
    """The fixture owing a J with the earliest deadline, else None."""
    owing = [r for r in rows if r["j_deadline_utc"] is not None]
    return min(owing, key=lambda r: r["j_deadline_utc"]) if owing else None


def render_row(r, now):
    h = (f"T-{r['hours_to_kickoff']:6.0f}h" if r["hours_to_kickoff"] is not None
         else (r["date"] or ""))
    ko = f"ko {_fmt(r['kickoff_utc'])}" if r["kickoff_utc"] else "ko unconfirmed"
    lines = [f"{h:10} {r['phase']:12} {r['match_id']}  {r['fixture']}  {ko}"]
    if r["j_deadline_utc"] is not None:
        left = r["minutes_to_deadline"] / 60.0
        risk = ("  ** AT RISK: not enough time left for evidence, J and the "
                "push **" if r["at_risk"] else "")
        lines.append(f"{'':10} J deadline {_fmt(r['j_deadline_utc'])} (aim to be "
                     f"done by {_fmt(r['finish_by_utc'])}; {left:.1f}h left){risk}")
    lines.append(f"{'':10} next: {r['next']}")
    if r["lost"]:
        lines.append(f"{'':10} LOST: {r['lost']}")
    return lines


def render_summary(rows, now):
    owing = [r for r in rows if r["j_deadline_utc"] is not None]
    head = f"NOW {_fmt(now)}"
    if not owing:
        return f"{head} | no J owed before a deadline"
    u = most_urgent(rows)
    risk = sum(1 for r in owing if r["at_risk"])
    return (f"{head} | J owed: {len(owing)} fixture(s) | most urgent: "
            f"{u['match_id']}, J deadline {_fmt(u['j_deadline_utc'])} "
            f"({u['minutes_to_deadline'] / 60.0:.1f}h left)"
            + (f" | AT RISK: {risk}" if risk else ""))


def _selfcheck():
    import tempfile
    from pathlib import Path

    from football_ai.competitions.epl_2026_27 import CONFIG

    fails = []
    KO = "2026-09-05T14:00:00+00:00"
    before = datetime(2026, 9, 1, tzinfo=timezone.utc)
    after = datetime(2026, 9, 6, tzinfo=timezone.utc)

    def lock(mid, with_j=False, version=1):
        payload = {"match_id": mid, "kickoff_utc": KO,
                   "teams": {"home": "H", "away": "A"},
                   "probs_90": {"home": .4, "draw": .3, "away": .3}}
        if with_j:
            payload["j"] = {"probs_90": payload["probs_90"]}
        store_io.append("locks", {"version": version, "payload": payload},
                        era=CONFIG.era, match_id=mid, version=version)

    def price(mid):
        store_io.append("odds", {"match_id": mid, "book": "b", "market": "1x2",
                                 "line": None, "outcomes": ["home"],
                                 "odds": [2.0], "fetched_at": KO},
                        era=CONFIG.era, match_id=mid)

    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            cases = {}
            cases["UNLOCKED"] = "u"
            lock("m"); cases["M_LOCKED"] = "m"
            lock("j", with_j=True); cases["J_LOCKED"] = "j"
            lock("f"); price("f"); cases["J_FORECLOSED"] = "f"
            lock("p", with_j=True); price("p"); cases["PRICED"] = "p"
            for want, mid in cases.items():
                got = phase(CONFIG, mid, now=before, kickoff_utc=KO)
                if got != want:
                    fails.append(f"{mid!r} should be {want}, got {got}")

            # the clock alone moves a locked fixture on
            if phase(CONFIG, "j", now=after, kickoff_utc=KO) != "KICKED_OFF":
                fails.append("a locked fixture past kickoff must be KICKED_OFF")
            store_io.append("results", {"match_id": "j", "score90": [1, 0]},
                            era=CONFIG.era, match_id="j")
            if phase(CONFIG, "j", now=after, kickoff_utc=KO) != "SETTLED":
                fails.append("a result must move the fixture to SETTLED")

            # losses are reported, never silent
            if not next_action(CONFIG, "f", now=before, kickoff_utc=KO)["lost"]:
                fails.append("a foreclosed J must be reported as lost — a "
                             "silent loss is indistinguishable from a choice")
            na = next_action(CONFIG, "m", now=after, kickoff_utc=KO)
            if not na["lost"]:
                fails.append("an M-locked fixture that kicked off without J "
                             "has permanently lost it and must say so")
            un = next_action(CONFIG, "u", now=after, kickoff_utc=KO)
            if "permanently unlockable" not in un["next"]:
                fails.append("an unlocked fixture past kickoff is gone, and "
                             "the driver must not suggest locking it")

            # a fixture with no confirmed kickoff is judged by its date:
            # a past date is gone, a future one must say the time is missing
            gone = next_action(CONFIG, "u", now=after, date="2026-09-05")
            if "permanently unlockable" not in gone["next"] or not gone["lost"]:
                fails.append("an unlocked fixture whose DATE has passed is "
                             "gone even with no kickoff time on record")
            soon = next_action(CONFIG, "u", now=before, date="2026-09-05")
            if "no confirmed kickoff" not in soon["next"] or soon["lost"]:
                fails.append("a future fixture with no kickoff must say its "
                             "time is unconfirmed, not invite a lock")

            # every phase has an action; none is a dead end
            for ph in PHASES:
                if ph not in _NEXT or len(_NEXT[ph]) < 20:
                    fails.append(f"phase {ph} has no next action")
            # a next step must never offer a hard-stop action, and every ops
            # command it names must be a real one
            from football_ai.competitions import ops as _ops
            for ph, text in _NEXT.items():
                if "ops snapshot" in text and "do NOT" not in text:
                    fails.append(f"{ph}: a next step must not suggest an "
                                 f"interactive ops snapshot")
                for act in re.findall(r"\{ops\} ([a-z][a-z-]*)", text):
                    if act not in _ops.ACTIONS:
                        fails.append(f"{ph}: names `ops {act}`, which does not exist")

            rows = matchweek(CONFIG, [
                {"match_id": "j", "gameweek": 3, "kickoff_utc": KO,
                 "home_team": "H", "away_team": "A"},
                {"match_id": "m", "gameweek": 4, "kickoff_utc": KO,
                 "home_team": "H", "away_team": "A"}], gameweek=3, now=before)
            if len(rows) != 1 or rows[0]["match_id"] != "j":
                fails.append(f"matchweek filter wrong: {rows}")

            # --- deadlines, the degraded J and the judged phase --------------
            def jlock(mid, k):
                payload = {"match_id": mid, "kickoff_utc": KO,
                           "teams": {"home": "H", "away": "A"},
                           "probs_90": {"home": .4, "draw": .3, "away": .3},
                           "j": {"probs_90": {"home": .4, "draw": .3, "away": .3},
                                 "runner": {"k_valid": k}}}
                store_io.append("locks", {"version": 1, "payload": payload},
                                era=CONFIG.era, match_id=mid, version=1)
            jlock("deg", 1); jlock("ok2", 2)
            if phase(CONFIG, "deg", now=before, kickoff_utc=KO) != "J_DEGRADED":
                fails.append("a J with k_valid=1 will be treated as absent by "
                             "the scheduled job: the phase must say so")
            if phase(CONFIG, "ok2", now=before, kickoff_utc=KO) != "J_LOCKED":
                fails.append("k_valid=2 is a usable J")
            store_io.append("judgments", {"match_id": "m"}, era=CONFIG.era, match_id="m")
            if phase(CONFIG, "m", now=before, kickoff_utc=KO) != "M_LOCKED":
                fails.append("a judgment written with no resolved J must NOT hide "
                             "the J the fixture still owes")
            store_io.append("judgments", {"match_id": "ok2"}, era=CONFIG.era, match_id="ok2")
            if phase(CONFIG, "ok2", now=before, kickoff_utc=KO) != "JUDGED":
                fails.append("a locked J plus a judgment record is JUDGED")
            fx = lambda mid, ko: {"match_id": mid, "gameweek": 9, "kickoff_utc": ko,
                                  "home_team": "H", "away_team": "A", "date": ko[:10]}
            now2 = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
            lines_rows = matchweek(CONFIG, [fx("m", KO), fx("deg", KO), fx("ok2", KO),
                                            fx("u", "2026-09-05T19:00:00+00:00")], now=now2)
            by = {r["match_id"]: r for r in lines_rows}
            if by["m"]["j_deadline_utc"] != datetime(2026, 9, 5, 11, 47, tzinfo=timezone.utc):
                fails.append(f"deadline for a 14:00Z kickoff is 11:47Z, got {by['m']['j_deadline_utc']}")
            if by["ok2"]["j_deadline_utc"] is not None:
                fails.append("a fixture with a usable J owes no deadline")
            if by["u"]["j_deadline_utc"] != datetime(2026, 9, 5, 16, 47, tzinfo=timezone.utc):
                fails.append("an unlocked 19:00Z fixture is due at 16:47Z")
            late = matchweek(CONFIG, [fx("m", KO)], now=datetime(2026, 9, 5, 11, 20, tzinfo=timezone.utc))[0]
            if not late["at_risk"]:
                fails.append("27 minutes before the deadline is not enough time: AT RISK")
            if most_urgent(lines_rows)["match_id"] != "m":
                fails.append("most_urgent must be the earliest deadline owing a J")
            text = "\n".join(render_row(by["m"], now2))
            if "epl-" in text or "J deadline 09-05 11:47Z" not in text \
                    or "m  H v A" not in text or "next:" not in text:
                fails.append(f"a rendered row must carry the match id, the deadline "
                             f"and the next step: {text!r}")
            if "no J owed" not in render_summary([by["ok2"]], now2):
                fails.append("the summary must say when nothing is owed")
        finally:
            store_io.STORE_DIR = old

    # the unpublished-ledger warning: shown when changes are not on origin,
    # silent when clean
    from football_ai.competitions import ops_tools
    real_state = ops_tools._ledger_state
    try:
        ops_tools._ledger_state = lambda: ("unpushed", "1 local commit(s) touching the ledger")
        note = ledger_note()
        if not note or "publish" not in note or "unpushed" not in note:
            fails.append(f"ledger_note must tell the operator to publish: {note!r}")
        ops_tools._ledger_state = lambda: ("clean", "")
        if ledger_note() is not None:
            fails.append("ledger_note must be silent when the ledger is clean")
        ops_tools._ledger_state = lambda: ("unchecked", "no git")
        if ledger_note() is not None:
            fails.append("an unverifiable ledger state must not produce a warning")
    finally:
        ops_tools._ledger_state = real_state
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase and next legal action for each fixture. By default "
                    "only what needs attention: anything locked and unsettled, "
                    "and unlocked fixtures inside the look-ahead window. The "
                    "rest is counted in the footer, never silently dropped.")
    ap.add_argument("--competition", default="epl-2026-27")
    ap.add_argument("--gameweek", type=int)
    ap.add_argument("--days", type=float, default=14,
                    help="look-ahead window for unlocked fixtures (default 14)")
    ap.add_argument("--all", action="store_true",
                    help="list every fixture, including settled and lapsed")
    ap.add_argument("--next", action="store_true",
                    help="only the single most urgent fixture and its next step")
    args = ap.parse_args(argv)

    from football_ai.competitions.ops import _competition
    comp = _competition(args.competition)
    pause = comp.get("paused")
    if pause:
        print(f"** PAUSED by owner since {pause['since']} ({pause['reason']}); "
              f"resume: {pause['resume']}. The list below is what would be "
              f"actionable, not what is being done. **")
    rows = matchweek(comp["config"], comp.get("upcoming_fixtures") or [],
                     gameweek=args.gameweek)
    now = datetime.now(timezone.utc)
    note = ledger_note()
    if note:
        print(note)
    shown, counts = select(rows, now, days=args.days, show_all=args.all)
    print(render_summary(shown, now))
    if args.next:
        u = most_urgent(shown) or next(
            (r for r in shown if r["phase"] not in ("JUDGED", "KICKED_OFF", "SETTLED")), None)
        for line in (render_row(u, now) if u else ["nothing to do now"]):
            print(line)
        return
    for r in shown:
        for line in render_row(r, now):
            print(line)
    if not args.all:
        print(f"-- {counts['settled']} settled; {counts['lapsed']} lapsed unlocked "
              f"(kickoff passed with no lock, gone for good); {counts['later']} "
              f"unlocked later than {args.days:g} days (--all lists everything)")

if __name__ == "__main__":
    main()
