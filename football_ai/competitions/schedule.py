"""When the unattended job acts, as arithmetic.

The scheduled job (`.github/workflows/epl-matchday.yml`, which runs
`ops matchday`) is what ends the window in which a fixture's J can still be
formed independently: any run that calls the odds API writes a price for
every locked fixture inside its closing window, whether or not it has a J,
and a J formed after that is contaminated. So "how long do I have" is a
function of the cron schedule and the closing window. It used to be worked
out by hand, and was worked out wrongly more than once.

Since 2026-10-10 a run calls the API only when a price is DUE (ops.fetch_due:
a closing price for a fixture 10-45 minutes from kickoff, or the entry price
of a fixture whose J was just locked). Whether a given tick calls therefore
depends on other fixtures too, so the J deadline below stays the
conservative one: the first tick inside the closing window, the earliest
moment a run could price a fixture that has no J.

Everything here derives from the same constants `ops` runs with, and
`_selfcheck` parses the workflow file and requires its cron to agree, so the
deadline cannot drift from what the job really does.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The cron in the workflow file is "7,27,47 10-19 * * *": minutes 7, 27 and
# 47 of hours 10..19 UTC (owner-approved 2026-10-10, AGENT_DISPATCH section 7).
#   * Every 20 minutes, so the 35-minute closing-due window below holds at
#     least one tick, and two for every standard Premier League kickoff
#     (checked in _selfcheck). GitHub starts scheduled runs late or drops
#     them under load; a second tick in the window is the margin for that.
#   * Off the top of the hour, where GitHub's load (and so its delay) is
#     worst.
#   * Hours 10..19: kickoffs run 11:30-20:00 UTC (12:30-20:00 UK local across
#     BST and GMT), and the last closing-due tick a 20:15 UTC kickoff needs
#     is 19:47. A kickoff after 20:32 UTC would get no closing price;
#     _selfcheck fails if a registered fixture list ever holds one.
TICK_MINUTES = (7, 27, 47)
TICK_HOURS_UTC = tuple(range(10, 20))

# Flag `ops matchday` passes to `snapshot` (see ops._matchday): only fixtures
# kicking off within this many hours are priced at all.
ONLY_WITHIN_HOURS = 100

# Inside this many minutes before kickoff, a locked fixture is priced even
# with no J. Equal to ops.DEFAULT_CLOSING_WINDOW_MIN (asserted in _selfcheck).
CLOSING_WINDOW_MIN = 150

# When a run may spend quota (ops.fetch_due), owner-approved 2026-10-10:
#   closing due -- a locked fixture kicks off in [now+10, now+45] minutes and
#     has no closing-stamped 1x2 snapshot fetched at or after kickoff-60;
#   entry due -- a fixture with M and a usable J (k_valid >= 2) kicks off
#     within ENTRY_HORIZON_HOURS and has no snapshot of any kind.
# The lower bound leaves the run time to finish before kickoff; the
# freshness bound means one price per fixture inside its last hour is
# enough, so the second tick in the window does not call again.
CLOSING_DUE_MIN = (10, 45)
CLOSING_FRESH_MIN = 60
ENTRY_HORIZON_HOURS = ONLY_WITHIN_HOURS
# An odds row fetched this long after a fixture's J was locked is taken as
# proof that a call already ran with that J on `main` (a lock is pushed in
# the same session, minutes after it is written). If that call did not
# price the fixture, it is not in the API's response (club-name mapping,
# postponement, no line yet), and calling again every tick would burn quota
# for nothing; its closing call still comes, and the miss is reported.
ENTRY_PUSH_SLACK_MIN = 60

# GitHub Actions minutes. In the private working repo every job is billed in whole
# minutes, and a run takes about 20-35 s (64 s at worst, 2026-10-06..10), so
# a run costs one minute. Owner's cap: 1000 a month. The scheduled runs of a
# 31-day month must leave ACTIONS_RESERVE_MIN of it for manual dispatches
# and the odd run past a minute (asserted in _selfcheck).
ACTIONS_MINUTES_CAP = 1000
ACTIONS_RESERVE_MIN = 50

# Planning figures, not measurements of the job. Evidence validation, J
# (its samples run in parallel) and the push have to finish inside the
# budget for a fixture to count as feasible; the buffer covers GitHub starting
# a scheduled run a few minutes late and a retry that goes wrong.
J_RUN_BUDGET_MIN = 45
J_SAFETY_BUFFER_MIN = 60


def _utc(dt):
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_tick(dt):
    """The first scheduled tick at or after `dt` (UTC)."""
    t = _utc(dt).replace(second=0, microsecond=0)
    day = t.replace(hour=0, minute=0)
    for offset in range(0, 3):
        d = day + timedelta(days=offset)
        for hour in TICK_HOURS_UTC:
            for minute in TICK_MINUTES:
                cand = d.replace(hour=hour, minute=minute)
                if cand >= t:
                    return cand
    raise AssertionError("no tick within three days -- TICK_HOURS_UTC is empty")


def closing_tick(kickoff):
    """The first tick inside the fixture's closing window -- the earliest run
    that could price it with no J (any due call prices every locked fixture
    inside its closing window) -- or None when the job never ticks there (a
    kickoff in the small hours: the cron stops at 19:47, so the next tick is
    after the match)."""
    t = next_tick(_utc(kickoff) - timedelta(minutes=CLOSING_WINDOW_MIN))
    return t if t <= _utc(kickoff) else None


def closing_due_ticks(kickoff):
    """The ticks at which the fixture's own closing price falls due (kickoff
    CLOSING_DUE_MIN minutes ahead). An empty list means it gets no closing
    price unless some other call happens to land inside its closing window --
    a CLV problem, not a J one."""
    lo, hi = CLOSING_DUE_MIN
    ko = _utc(kickoff)
    out, t = [], next_tick(ko - timedelta(minutes=hi))
    while t <= ko - timedelta(minutes=lo):
        out.append(t)
        t = next_tick(t + timedelta(minutes=1))
    return out


def j_deadline(kickoff):
    """The moment by which a J must be locked AND pushed: the closing tick,
    or the kickoff itself when no tick falls in the window (lock_match and
    amend_lock refuse after kickoff anyway)."""
    t = closing_tick(kickoff)
    return t if t is not None else _utc(kickoff)


def finish_by(kickoff):
    """The deadline to work to: the hard one, less the safety buffer."""
    return j_deadline(kickoff) - timedelta(minutes=J_SAFETY_BUFFER_MIN)


def minutes_left(kickoff, now):
    """Minutes from `now` to the hard J deadline (negative once past)."""
    return (j_deadline(kickoff) - _utc(now)).total_seconds() / 60.0


def feasible(kickoff, now):
    return minutes_left(kickoff, now) >= J_RUN_BUDGET_MIN + J_SAFETY_BUFFER_MIN


def _selfcheck():
    fails = []

    def at(day, hh, mm):
        return datetime(2026, 10, day, hh, mm, tzinfo=timezone.utc)

    # kickoff -> expected hard J deadline, worked by hand from the closing
    # window (kickoff - 150 min) and the ticks :07/:27/:47 of hours 10..19
    cases = [
        (at(10, 11, 30), at(10, 10, 7)),   # window opens 09:00 -> first tick 10:07
        (at(10, 14, 0), at(10, 11, 47)),   # 11:30 -> 11:47
        (at(10, 16, 30), at(10, 14, 7)),   # 14:00 -> 14:07
        (at(11, 13, 0), at(11, 10, 47)),   # 10:30 -> 10:47
        (at(11, 15, 30), at(11, 13, 7)),   # 13:00 -> 13:07
        (at(12, 19, 0), at(12, 16, 47)),   # 16:30 -> 16:47
        (at(12, 21, 0), at(12, 18, 47)),   # 18:30 -> 18:47
        (at(10, 10, 30), at(10, 10, 7)),   # 08:00 is before the first tick of the day
        (at(10, 9, 0), at(10, 9, 0)),      # no tick between 06:30 and kickoff
        (at(11, 1, 0), at(11, 1, 0)),      # window opens 22:30: no tick left that day,
        (at(11, 3, 0), at(11, 3, 0)),      # the next is after kickoff -> deadline = kickoff
    ]
    for ko, want in cases:
        got = j_deadline(ko)
        if got != want:
            fails.append(f"j_deadline({ko:%m-%d %H:%MZ}) should be "
                         f"{want:%m-%d %H:%MZ}, got {got:%m-%d %H:%MZ}")
    # the deadline is never after the point where the fixture is inside its window
    for ko, _ in cases:
        if not (j_deadline(ko) <= _utc(ko)):
            fails.append(f"deadline after kickoff for {ko}")
    if closing_tick(at(11, 1, 0)) is not None or closing_tick(at(10, 11, 30)) != at(10, 10, 7):
        fails.append("closing_tick: None when no tick falls inside the window, "
                     "the tick itself otherwise")
    ko = at(10, 14, 0)
    if finish_by(ko) != j_deadline(ko) - timedelta(minutes=J_SAFETY_BUFFER_MIN):
        fails.append("finish_by must be the deadline less the buffer")
    if not feasible(ko, at(10, 9, 0)) or feasible(ko, at(10, 10, 30)):
        fails.append("feasible() must be true with time for budget + buffer, "
                     "false without")
    if minutes_left(ko, at(10, 11, 47)) != 0 or minutes_left(ko, at(10, 12, 7)) >= 0:
        fails.append("minutes_left must reach zero at the deadline and go negative")
    if (next_tick(at(10, 10, 7)) != at(10, 10, 7) or next_tick(at(10, 10, 8)) != at(10, 10, 27)
            or next_tick(at(10, 19, 48)) != at(11, 10, 7)):
        fails.append("next_tick is 'at or after': 10:07 stays, 10:08 moves to 10:27, "
                     "19:48 to the next day's 10:07")
    if closing_due_ticks(at(10, 14, 0)) != [at(10, 13, 27), at(10, 13, 47)]:
        fails.append(f"a 14:00Z kickoff is closing-due at 13:27 and 13:47, got "
                     f"{closing_due_ticks(at(10, 14, 0))}")

    # Every standard Premier League kickoff (UK local, in BST and in GMT)
    # must get its closing price from two ticks, so one late or dropped run
    # does not lose it; every kickoff in a registered fixture list, from one.
    uk_slots = [(12, 0), (12, 30), (13, 0), (13, 30), (14, 0), (15, 0), (15, 30),
                (16, 30), (17, 30), (19, 30), (19, 45), (20, 0)]
    for hh, mm in uk_slots:
        for offset_h in (1, 0):     # BST is UTC+1, GMT is UTC
            ko = at(10, hh, mm) - timedelta(hours=offset_h)
            n = len(closing_due_ticks(ko))
            if n < 2:
                fails.append(f"a {hh:02d}:{mm:02d} UK kickoff ({ko:%H:%M}Z) has "
                             f"{n} closing-due tick(s); the cron needs two there")
    from football_ai.competitions import ACTIVE
    for comp in ACTIVE:
        for f in comp.get("upcoming_fixtures") or []:
            text = f.get("kickoff_utc")
            if not text:
                continue
            ko = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if not closing_due_ticks(ko):
                fails.append(f"{f['match_id']} (ko {ko:%m-%d %H:%MZ}) has no "
                             f"closing-due tick: it would get no closing price")

    # --- tied to the things the job actually runs with ---------------------
    from football_ai.competitions import ops
    if CLOSING_WINDOW_MIN != ops.DEFAULT_CLOSING_WINDOW_MIN:
        fails.append("schedule.CLOSING_WINDOW_MIN drifted from ops.DEFAULT_CLOSING_WINDOW_MIN")
    if ONLY_WITHIN_HOURS != ops.MATCHDAY_ONLY_WITHIN_HOURS:
        fails.append("schedule's matchday horizon drifted from ops's")
    if not (0 < CLOSING_DUE_MIN[0] < CLOSING_DUE_MIN[1] < CLOSING_FRESH_MIN
            <= CLOSING_WINDOW_MIN):
        fails.append("the closing-due window must sit inside the freshness bound, "
                     "and that inside the closing window (else a due price would "
                     "not be stamped closing, or would never count as fresh)")
    per_month = 31 * len(TICK_MINUTES) * len(TICK_HOURS_UTC)
    if per_month > ACTIONS_MINUTES_CAP - ACTIONS_RESERVE_MIN:
        fails.append(f"the cron schedules {per_month} runs in a 31-day month, each "
                     f"billed at least a minute: over the owner's "
                     f"{ACTIONS_MINUTES_CAP}-minute cap less the reserve")
    root = Path(__file__).resolve().parents[2]
    yml = root / ".github" / "workflows" / "epl-matchday.yml"
    if yml.exists():
        crons = re.findall(r'cron:\s*"([\d,]+)\s+(\d+)-(\d+)\s+\*\s+\*\s+\*"',
                           yml.read_text(encoding="utf-8"))
        if len(crons) != 1:
            fails.append("could not read exactly one cron line of the form "
                         "'M,M,M H-H * * *' in epl-matchday.yml; the deadline "
                         "arithmetic is no longer checked against it")
        else:
            mins, lo, hi = crons[0]
            minutes = tuple(int(x) for x in mins.split(","))
            hours = tuple(range(int(lo), int(hi) + 1))
            if (minutes, hours) != (TICK_MINUTES, TICK_HOURS_UTC):
                fails.append(f"epl-matchday.yml's cron ({mins} {lo}-{hi}) no "
                             f"longer matches TICK_MINUTES/TICK_HOURS_UTC")
    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
