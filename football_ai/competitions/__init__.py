"""Registry of ACTIVE (non-archived) competitions, so session-opening code
has ONE place to ask "what's due across everything I'm tracking right now"
instead of the owner having to remember to ask about each competition by
name (CLAUDE.md's whole reason for `core.agenda` existing in the first
place -- see its "第一個動作": the owner should never have to
remember to ask).

WHY THIS FILE EXISTS (2026-07-24): `core.agenda.due_items()` needs an
explicit `config` per competition; `python -m football_ai.core.agenda`
alone only ever checks the WC2026 default path. Without this registry,
adding a new competition (EPL, then presumably others later) would mean
either hardcoding it into CLAUDE.md's session-opening instructions by name
forever, or -- worse -- a session silently not knowing to check it at all.
`all_due_items()` below is what CLAUDE.md's session-opening rule actually
calls; add a new competition here once, and every future session picks it
up automatically without another CLAUDE.md edit.
"""
from football_ai.core import agenda
from football_ai.competitions import (
    efl_championship_2026_27, epl_2026_27, epl_2026_27_fixtures)

ACTIVE = [
    {
        "name": "英超 2026-27",
        "config": epl_2026_27.CONFIG,
        "upcoming_fixtures": epl_2026_27_fixtures.FIXTURES,
        # the two competition-specific callables `ops.run_j()` needs, so it
        # stays generic: how this competition amends a lock, and where its
        # own fitted goal curve comes from. Registering them here is what
        # keeps "a second league costs a registry entry, not a rewrite"
        # true for the J step as well as for snapshot/settle.
        "amend_lock": epl_2026_27.amend_lock,
        "goal_curve": epl_2026_27.goal_curve,
        # what `ops lock` needs: the M instrument and the ratings it reads
        "lock_match": epl_2026_27.lock_match,
        "current_ratings": epl_2026_27.current_ratings,
        # To pause a competition, add a "paused" entry here -- a recorded
        # owner decision with `since`, `reason` and `resume` keys, read by
        # every session-opening report. While set, the per-fixture "not
        # locked" reminders collapse into one line saying what lapses;
        # nothing is blocked, and settlement / J / closing-price items still
        # appear. Cheap to hold, and the cost is stated, not hidden: every
        # unlocked fixture that kicks off is gone for good.
        # History: paused 2026-10-06 (owner had no time); owner authorized
        # resuming the same day, for MW6 onward (10/10).
    },
    {
        # Trial window (owner authorization 2026-10-10, AGENT_DISPATCH
        # section 7): M and closing-price scoring only; J refuses. What must
        # happen before the pause is lifted is the ACTIVATION CHECKLIST in
        # efl_championship_2026_27.py.
        "name": "英冠 2026-27",
        "config": efl_championship_2026_27.CONFIG,
        "upcoming_fixtures": efl_championship_2026_27.FIXTURES,
        "amend_lock": efl_championship_2026_27.amend_lock,
        "goal_curve": efl_championship_2026_27.goal_curve,
        "lock_match": efl_championship_2026_27.lock_match,
        "current_ratings": efl_championship_2026_27.current_ratings,
        "paused": {
            "since": "2026-10-10",
            "reason": "registered paused; M-only trial skeleton, no fixture "
                      "list or calibration record yet",
            "resume": "owner decides, after the ACTIVATION CHECKLIST in "
                      "competitions/efl_championship_2026_27.py",
        },
    },
]

# Per-fixture reminders that a pause replaces with a single summary line.
_PAUSE_COLLAPSED = ("upcoming, not locked:", "in the next week, not locked:")


def all_due_items(now=None):
    """Due items across every ACTIVE competition, each item prefixed with
    its competition name so a multi-competition list stays legible.
    Returns a flat, priority-sorted list of (priority, line) tuples --
    same shape as `core.agenda.due_items()` itself, just fanned out.
    """
    items = []
    for comp in ACTIVE:
        pause = comp.get("paused")
        lapsing = 0
        for pri, line in agenda.due_items(
                now, config=comp["config"],
                upcoming_fixtures=comp.get("upcoming_fixtures")):
            if pause and line.startswith(_PAUSE_COLLAPSED):
                lapsing += 1
                continue
            items.append((pri, f"[{comp['name']}] {line}"))
        if pause:
            tail = (f"; {lapsing} fixture(s) kick off in the next 7 days and "
                    f"will lapse unlocked" if lapsing else "")
            items.append((2, f"[{comp['name']}] PAUSED by owner since "
                             f"{pause['since']} ({pause['reason']}){tail}. "
                             f"Resume: {pause['resume']}"))
    return sorted(items)


def matchflow_next(competition_id):
    """(lines, error): the most urgent fixture of one competition and its next
    legal step, as `matchflow --next` prints it. An exception comes back as an
    error string rather than propagating, because the opening report must
    still say everything else it knows."""
    import contextlib
    import io

    from football_ai.competitions import matchflow
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            matchflow.main(["--competition", competition_id, "--next"])
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    return buf.getvalue().rstrip().splitlines(), None


def opening_report(now=None):
    """The session-opening report: what is due across every ACTIVE competition,
    then the most urgent fixture and its next step. One command, so the first
    look is not assembled by hand from two.

    The agenda's per-fixture "not locked" reminders say less than the matchflow
    rows that follow (no deadline, no next step), so they are folded into
    matchflow's count -- but only when matchflow ran. If it failed, the error
    is printed and the reminders stay: a broken tool must not also remove
    the fallback."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    items = all_due_items(now)
    flows = {}
    for comp in ACTIVE:
        flows[comp["name"]] = matchflow_next(comp["config"].competition_id)
    folded = {name for name, (lines, err) in flows.items() if err is None}
    out = [f"=== active competitions @ {now.isoformat()[:16]}Z ==="]
    if not ACTIVE:
        out.append("  (no ACTIVE competitions registered)")
    for pri, line in items:
        name = line[1:line.index("]")] if line.startswith("[") else ""
        if name in folded and line.split("] ", 1)[-1].startswith(_PAUSE_COLLAPSED):
            continue
        out.append(f"  [P{pri}] {line}")
    for name, (lines, err) in flows.items():
        out.append(f"--- {name}: most urgent fixture and its next step ---")
        if err is not None:
            out.append(f"  matchflow FAILED ({err}); run "
                       f"`python -m football_ai.competitions.matchflow` by hand")
        else:
            out.extend(f"  {l}" for l in lines)
    out.append("every fixture: python -m football_ai.competitions.matchflow")
    return out


def _selfcheck():
    fails = []
    if not ACTIVE:
        fails.append("ACTIVE should not be empty once at least one "
                     "competition (EPL) has been onboarded")
    for comp in ACTIVE:
        if "config" not in comp or "name" not in comp:
            fails.append(f"ACTIVE entry missing required keys: {comp}")
        # ops.run_j() resolves these two by name; a competition registered
        # without them fails at the J step, mid-window, with no lead time
        for key in ("amend_lock", "goal_curve", "lock_match", "current_ratings"):
            if not callable(comp.get(key)):
                fails.append(f"ACTIVE entry {comp.get('name')!r} has no "
                             f"callable {key!r} — ops.run_j() cannot drive it")
    for comp in ACTIVE:
        pause = comp.get("paused")
        if pause and not {"since", "reason", "resume"} <= set(pause):
            fails.append(f"{comp['name']}: a pause must carry since / reason / "
                         f"resume, so it is a recorded decision and not a mute")
    try:
        items = all_due_items()
    except Exception as e:
        fails.append(f"all_due_items() raised against the real store: {e}")
        return fails
    if not isinstance(items, list):
        fails.append("all_due_items() must return a list")
    elif items and not all(
            isinstance(t, tuple) and len(t) == 2 and t[1].startswith("[")
            for t in items):
        fails.append(f"all_due_items() entries must be (priority, "
                     f"'[competition name] ...') tuples: {items[:3]}")

    # opening_report: folds the per-fixture reminders only when matchflow ran
    from football_ai.competitions import matchflow
    real = matchflow.main
    try:
        text = "\n".join(opening_report())
        if "matchflow FAILED" in text:
            fails.append(f"opening_report: matchflow failed against the real store: {text[:300]}")
        if "not locked:" in text and "FAILED" not in text:
            fails.append("opening_report must fold the per-fixture 'not locked' "
                         "reminders into matchflow's rows when matchflow ran")

        def boom(*a, **k):
            raise RuntimeError("planted")
        matchflow.main = boom
        text = "\n".join(opening_report())
        if "matchflow FAILED (RuntimeError: planted)" not in text:
            fails.append("opening_report must say loudly when matchflow fails")
    except Exception as e:
        fails.append(f"opening_report raised: {type(e).__name__}: {e}")
    finally:
        matchflow.main = real
    return fails
