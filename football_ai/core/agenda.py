"""Deterministic due-list for the live window — the anti-forgetting layer.

WHY (owner, 2026-07-09): "我怕我會忘記叫你升級/使用工具". Sessions cannot
self-start, so the contract is: the OWNER only has to show up; the AI must
figure out what is due. This module computes the due-list mechanically from
locks/results/kickoffs + today's clock, so CLAUDE.md can instruct every
session to run it FIRST and proactively report — no human memory required.

Run: python -m football_ai.core.agenda

MULTI-COMPETITION NOTE (2026-07-24): `due_items()` takes an optional
`config=None` (a `core.competition.CompetitionConfig`). `config=None` is
byte-behavior-identical to this module's pre-2026-07-24 form -- it reads
V2_DIR raw files and keeps the QF/SF/final bracket-feeder logic and the
EXPECTED calendar exactly as before (both are genuinely knockout-bracket
concepts with no round-robin equivalent, research/audit/final_report.md's
Executive Summary: this is the one file where bracket topology is baked
into control flow, not data). A passed `config` uses a completely
different, simpler path: fixtures come from `registry.build_fixtures()`
(store-based, already config-aware) instead of scanning raw files, there is
no bracket-feeder or EXPECTED-calendar concept at all, and an optional
`upcoming_fixtures` list (e.g. `competitions/epl_2026_27_fixtures.FIXTURES`)
drives "known fixture, not locked yet" reminders instead of a hardcoded
calendar dict.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from football_ai.core import registry
from football_ai.core.paths import v2_archive_dir
from football_ai.store import io as store_io

V2_DIR = v2_archive_dir()

# expected remaining fixtures (match_num -> expected date UTC, stage) —
# pairings become known as feeders settle; dates from the official calendar
EXPECTED = {
    101: ("2026-07-14", "SF1 (w97 vs w98 expected)"),
    102: ("2026-07-15", "SF2 (w99 vs w100 expected)"),
    103: ("2026-07-18", "3rd place"),
    104: ("2026-07-19", "Final"),
}
MISSING = {92}
MAIN = range(89, 105)
ALIASES = {"r16_por_esp": 93}
# registered M-only matches: J is PERMANENTLY absent (analyst saw market
# numbers pre-lock — por_esp / m97 precedents). Never nag to add J here;
# an added-late j would be dropped by score.py's contaminated_j audit anyway.
M_ONLY = {"r16_por_esp", "m97_fra_mar"}


def _num(mid, payload):
    if payload.get("pilot"):
        return None
    if mid in ALIASES:
        return ALIASES[mid]
    import re
    m = re.match(r"^m(\d+)_", mid)
    return int(m.group(1)) if m and int(m.group(1)) in MAIN else None


# WC2026 bracket topology AS DATA (2026-08-28). This used to be two `if
# num in (...)` branches inside due_items' loop -- the generalization audit
# called it "the one file in the non-archived codebase where bracket
# topology is baked into actual control flow rather than data", and the one
# spot a config swap alone could not generalize. A future knockout window
# supplies the same shape from `CompetitionConfig.knockout.bracket` instead
# of editing this module.
#
# `require_score` PRESERVES AN ASYMMETRY FOUND IN THE ORIGINAL, rather than
# tidying it away: the semi-final check demanded a `score90` on each
# quarter-final, while the final/third-place check accepted the mere
# presence of a results entry. Those are not the same test -- results.json
# carries an entry for m92, which was never played to a recorded score --
# so normalizing them would have changed what the archived path reports.
# It is almost certainly an oversight (the intent in both cases is "the
# feeders have finished, so this pairing is known"), and anyone reviving a
# knockout window should use require_score=True throughout; it is inert
# here because all sixteen fixtures are long since locked and settled, and
# the loop skips locked fixtures before ever consulting it.
WC_BRACKET = {
    101: {"feeders": (97, 98, 99, 100), "require_score": True},
    102: {"feeders": (97, 98, 99, 100), "require_score": True},
    103: {"feeders": (101, 102), "require_score": False},
    104: {"feeders": (101, 102), "require_score": False},
}


def _feeders_done(num, bracket, locks, results):
    """True when every fixture this one depends on has finished, so its
    pairing is known and it can be locked. A fixture with no entry in
    `bracket` has no dependencies (a round-robin league fixture, or the
    first round of a bracket) and is therefore always ready."""
    node = bracket.get(num)
    if node is None:
        return True
    for feeder in node["feeders"]:
        info = locks.get(feeder)
        if not info or info["match_id"] not in results:
            return False
        if node.get("require_score") and "score90" not in results[info["match_id"]]:
            return False
    return True


def load_state():
    locks = {}
    for f in sorted((V2_DIR / "locks").glob("*.json")):
        if ".v" in f.name:
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        mid = doc["payload"]["match_id"]
        num = _num(mid, doc["payload"])
        if num is None:
            continue
        has_j = bool(doc["payload"].get("j"))
        for vf in sorted((V2_DIR / "locks").glob(f"{mid}.v*.json")):
            vdoc = json.loads(vf.read_text(encoding="utf-8"))
            has_j = has_j or bool(vdoc["payload"].get("j"))
        locks[num] = {"match_id": mid,
                      "kickoff": doc["payload"]["kickoff_utc"],
                      "has_j": has_j}
    results = json.loads((V2_DIR / "results.json").read_text(encoding="utf-8")) \
        if (V2_DIR / "results.json").exists() else {}
    return locks, results


def due_items(now=None, config=None, upcoming_fixtures=None):
    """List of (priority, line) strings. Deterministic; no network.

    `config=None` (default): WC2026 behavior, unchanged below -- bracket
    feeders, EXPECTED calendar, M_ONLY exemptions, post-final milestone.

    `config` (a `core.competition.CompetitionConfig`): a different,
    simpler path -- see `_due_items_for_config()`. `upcoming_fixtures` is
    an optional list of dicts with at least `match_id`/`date` (and
    ideally `home_team`/`away_team`), e.g.
    `competitions/epl_2026_27_fixtures.FIXTURES` -- known future fixtures
    that aren't locked yet get a reminder; without it, this function only
    reports on ALREADY-locked fixtures (settle/J-lock/action-tree)."""
    if config is not None:
        return _due_items_for_config(now, config, upcoming_fixtures)

    now = now or datetime.now(timezone.utc)
    locks, results = load_state()
    items = []

    for num, info in sorted(locks.items()):
        ko = datetime.fromisoformat(info["kickoff"])
        mid = info["match_id"]
        settled = mid in results and "score90" in results.get(mid, {})
        if ko < now and not settled:
            items.append((1, f"SETTLE 到期:{mid}(開球 {info['kickoff'][:16]} 已過)"
                             f" → 說「settle {mid.split('_')[0]}」"))
        if not info["has_j"] and now < ko and mid not in M_ONLY:
            hrs = (ko - now).total_seconds() / 3600
            flag = "⚠ 6h 死線內!" if hrs < 6 else ""
            items.append((1 if hrs < 12 else 2,
                          f"J 未鎖:{mid}(開球剩 {hrs:.0f}h){flag} → 乾淨 "
                          f"session 接盤前 amend 補 J(J basis 屬 Fable 驗收節點)"))
        if now < ko <= now + timedelta(hours=4):
            t75 = ko - timedelta(minutes=75)
            items.append((1, f"行動樹窗口:{mid} T-75 = {t75.isoformat()[:16]}Z"
                             f"(先發公布時裁決預登記分支)"))

    for num in sorted(MAIN):
        if num in locks or num in MISSING or num not in EXPECTED:
            continue
        date, stage = EXPECTED[num]
        feeders_done = _feeders_done(num, WC_BRACKET, locks, results)
        days = (datetime.fromisoformat(date + "T00:00:00+00:00") - now).days
        if feeders_done:
            items.append((1, f"配對已定但未鎖:m{num}({stage},{date})"
                             f" → 立即 auto_lock_m 鎖 M"))
        elif days <= 2:
            items.append((3, f"即將到來:m{num}({stage},{date})——配對確定"
                             f"後第一時間 auto_lock_m"))

    # post-final milestone — and it has to be able to STOP. Until
    # 2026-08-28 this fired on "the final is settled" alone, which stayed
    # true forever, so the WC agenda opened every session with a P1 for
    # three pieces of work finished on 2026-07-20. A due-list that cannot
    # retire an item trains its reader to skim past P1s, which is the exact
    # failure it exists to prevent. The completion test is the artifacts
    # themselves: the section-4 report exists, and the archive it was
    # supposed to be filed into exists.
    if 104 in locks and locks[104]["match_id"] in results:
        milestone_done = ((V2_DIR / "FINAL_REPORT.md").exists()
                          and (V2_DIR / "PROTOCOL.md").exists())
        if not milestone_done:
            items.append((1, "主樣本完賽 → §4 正式報告 → 刀二歸檔 → 刀三搬遷"
                             "(重構記錄見 git 3c7fcc2、188c85d;說「出最終報告,執行刀二」)"))
    if not items:
        items.append((9, "無到期節點。下一場開球見上方 locks 狀態;升級隊列見 "
                         "DECISION_PROTOCOL §10。"))
    return sorted(items)


def _due_items_for_config(now, config, upcoming_fixtures):
    """The round-robin-shaped due-list: settle/J-lock/action-tree reminders
    for already-LOCKED fixtures (via registry.build_fixtures(), store-based,
    already config-aware -- no raw-file scanning), plus an optional
    "known fixture, not locked yet" reminder for `upcoming_fixtures`.

    Deliberately does NOT have a bracket-feeder or EXPECTED-calendar
    equivalent -- those are knockout-format concepts with nothing to
    generalize to for a round-robin competition (see module docstring).
    """
    now = now or datetime.now(timezone.utc)
    fixtures = registry.build_fixtures(era=config.era, sample=config.sample)
    results = {r["data"].get("match_id"): r["data"]
               for r in store_io.read("results", era=config.era)}
    # fixtures that already have a market price recorded: a J written after
    # this point is contaminated by construction, and amend_lock refuses it
    priced = {r.get("match_id")
              for r in store_io.read("odds", era=config.era)
              if r.get("match_id")}
    items = []
    locked_ids = set()

    for f in fixtures:
        if f.get("status") != "locked":
            continue
        mid = f.get("match_id")
        ko_raw = f.get("kickoff_utc")
        if mid is None or ko_raw is None:
            continue
        locked_ids.add(mid)
        ko = datetime.fromisoformat(ko_raw)
        settled = mid in results and "score90" in results.get(mid, {})
        if ko < now and not settled:
            items.append((1, f"SETTLE due: {mid} (kickoff {ko_raw[:16]} passed)"))
        if not f.get("has_j") and now < ko:
            hrs = (ko - now).total_seconds() / 3600
            if mid in priced:
                # Not a to-do: a price for this fixture is already in the
                # store, so any J written now would be contaminated and
                # amend_lock refuses it. Nagging for something the system
                # itself will not accept is how a due-list teaches its
                # reader to skim past P1s. Reported once, at the bottom.
                items.append((4, f"J no longer available: {mid} — priced "
                                 f"before J was locked, so a clean J is "
                                 f"foreclosed (M-only for this fixture)"))
            else:
                flag = " -- URGENT, <6h to kickoff" if hrs < 6 else ""
                items.append((1 if hrs < 12 else 2,
                              f"J not locked: {mid} (kickoff in {hrs:.0f}h){flag}"))

    for uf in (upcoming_fixtures or []):
        mid = uf.get("match_id")
        if mid is None or mid in locked_ids:
            continue
        date_raw = uf.get("date")
        if not date_raw:
            continue
        date_dt = datetime.fromisoformat(
            date_raw if "T" in date_raw else date_raw + "T00:00:00+00:00")
        if date_dt.tzinfo is None:
            date_dt = date_dt.replace(tzinfo=timezone.utc)
        days = (date_dt - now).days
        if days < -1:
            continue  # well in the past and still unlocked -> not this
                      # function's job to flag a stale schedule entry
        who = f"{uf.get('home_team','?')} vs {uf.get('away_team','?')}"
        if days <= 2:
            items.append((2, f"upcoming, not locked: {mid} ({who}, {date_raw})"))
        elif days <= 7:
            items.append((3, f"in the next week, not locked: {mid} ({who}, {date_raw})"))

    if not items:
        items.append((9, f"no due items for era={config.era!r}."))
    return sorted(items)


def _selfcheck():
    fails = []

    # bracket-as-data: a node with unfinished feeders is not ready, a node
    # with finished feeders is, and a node absent from the bracket (any
    # round-robin fixture) is always ready
    locks_f = {97: {"match_id": "m97"}, 98: {"match_id": "m98"},
               99: {"match_id": "m99"}, 100: {"match_id": "m100"},
               101: {"match_id": "m101"}, 102: {"match_id": "m102"}}
    part = {"m97": {"score90": [1, 0]}, "m98": {"score90": [2, 1]}}
    full = dict(part, **{"m99": {"score90": [0, 0]}, "m100": {"score90": [3, 1]}})
    if _feeders_done(101, WC_BRACKET, locks_f, part):
        fails.append("a semi-final must not be ready while a quarter-final "
                     "is unsettled")
    if not _feeders_done(101, WC_BRACKET, locks_f, full):
        fails.append("a semi-final must be ready once every quarter-final "
                     "has a score")
    if not _feeders_done(89, WC_BRACKET, locks_f, {}):
        fails.append("a fixture with no feeders must always be ready")
    # the preserved asymmetry: 103/104 accept a results ENTRY without a score
    if not _feeders_done(103, WC_BRACKET, locks_f,
                         {"m101": {}, "m102": {}}):
        fails.append("the final's feeder test must stay as-found "
                     "(entry present, score not required)")

    _orig = _selfcheck_rest()
    return fails + _orig


def _selfcheck_rest():
    fails = []
    # frozen-clock scenarios against the real repo state are brittle;
    # verify pure logic with a synthetic now far in the future: everything
    # locked must demand settlement, or the next pairing must demand a lock,
    # or the whole main sample must be complete -- some action is always due
    # (2026-07-16: broadened after the repo's real state caught up to "both
    # semis settled, 3rd-place/final pairing now known but not yet locked",
    # a real due-item shape the original check didn't anticipate)
    # 2026-08-28: this used to assert that SOMETHING is always due. That
    # premise was itself part of the stale-milestone bug -- the post-final
    # item fired forever, so "something is always due" could never fail.
    # With the milestone able to retire, a finished-and-filed window
    # correctly has NOTHING due, and the honest invariant is the
    # conditional one: either work is outstanding, or the window is
    # genuinely complete and says so.
    items = due_items(datetime(2099, 1, 1, tzinfo=timezone.utc))
    locks_s, results_s = load_state()
    outstanding = any(
        num not in locks_s
        or "score90" not in results_s.get(locks_s[num]["match_id"], {})
        for num in MAIN if num not in MISSING)
    surfaced = any("SETTLE" in ln or "完賽" in ln or "未鎖" in ln
                   for _, ln in items)
    if outstanding and not surfaced:
        fails.append("far-future clock must surface unsettled/unlocked matches")
    if not outstanding and surfaced:
        fails.append(f"a complete, settled, filed window must have nothing "
                     f"due, got {[ln for _, ln in items]}")
    # priority sort holds
    pris = [p for p, _ in items]
    if pris != sorted(pris):
        fails.append("items must be priority-sorted")

    # --- config-driven (round-robin-shaped) path ---
    import tempfile
    from pathlib import Path
    from football_ai.core.competition import (
        CompetitionConfig, DataSources, EloTierTable, GateThresholds,
        HomeAdvantageModel, SampleDefinition,
    )
    era = "test-agenda-dynamic"
    cfg = CompetitionConfig(
        competition_id="test-league", era=era, format="round_robin",
        sample=SampleDefinition(mode="dynamic", match_id_pattern=r"^t(\d+)_"),
        home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
        elo_tiers=EloTierTable(default_k=20), mechanism_min_n=10,
        gate=GateThresholds(watch_pp=6.5, bet_pp=10.0, stake_u=1.0,
                             max_bets_per_match=2),
        data_sources=DataSources(rating_source="x", results_source="y",
                                  odds_book_default="pinnacle"))
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    def _lock(mid, kickoff, has_j):
        payload = {"match_id": mid, "pilot": False,
                   "teams": {"home": "A", "away": "B"},
                   "kickoff_utc": kickoff.isoformat()}
        if has_j:
            payload["j"] = {"probs_90": {"home": 0.5}}
        return {"locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
                "payload": payload, "sha256": "x"}

    old_store = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            past_unsettled = "t01_a_b"       # kickoff passed, no result -> SETTLE
            soon_no_j = "t02_c_d"            # kickoff in 3h, no J -> J-urgent
            fine = "t03_e_f"                 # kickoff in 20h, has J -> nothing
            store_io.append("locks", _lock(past_unsettled, now - timedelta(hours=2), True),
                             era=era, match_id=past_unsettled, version=1)
            store_io.append("locks", _lock(soon_no_j, now + timedelta(hours=3), False),
                             era=era, match_id=soon_no_j, version=1)
            store_io.append("locks", _lock(fine, now + timedelta(hours=20), True),
                             era=era, match_id=fine, version=1)

            upcoming = [
                {"match_id": "t04_g_h", "home_team": "G", "away_team": "H",
                 "date": (now + timedelta(days=1)).date().isoformat()},
                # already locked above -- must be skipped as a duplicate, not
                # double-reported as "upcoming, not locked"
                {"match_id": fine, "home_team": "E", "away_team": "F",
                 "date": (now + timedelta(days=1)).date().isoformat()},
            ]
            cfg_items = due_items(now, config=cfg, upcoming_fixtures=upcoming)
            lines = [ln for _, ln in cfg_items]

            if not any("SETTLE due: t01_a_b" in ln for ln in lines):
                fails.append(f"config path: missing SETTLE reminder for past-kickoff "
                             f"unsettled fixture: {lines}")
            if not any("J not locked: t02_c_d" in ln and "URGENT" in ln for ln in lines):
                fails.append(f"config path: missing urgent J-lock reminder "
                             f"(<6h to kickoff): {lines}")
            # a league has no action tree: by T-75 the closing price is in the
            # store, an amend would be contaminated, and the reminder named no
            # action. It must not come back as an unexplained P1.
            if any("action-tree" in ln for ln in lines):
                fails.append(f"config path: a round-robin competition must not "
                             f"emit the knockout action-tree reminder: {lines}")
            if any("t03_e_f" in ln for ln in lines):
                fails.append(f"config path: fixture with J locked and kickoff "
                             f"20h out should have no reminder at all: {lines}")
            if not any("upcoming, not locked: t04_g_h" in ln for ln in lines):
                fails.append(f"config path: missing upcoming-not-locked reminder "
                             f"for a fixture never locked: {lines}")
            if sum(1 for ln in lines if "t03_e_f" in ln) != 0:
                fails.append(f"config path: t03_e_f is already locked, must not "
                             f"also appear as an 'upcoming, not locked' duplicate: {lines}")
            if any("m<" in ln or "配對" in ln or "刀二" in ln for ln in lines):
                fails.append("config path must not emit any bracket/knockout-specific "
                             f"WC2026 phrasing: {lines}")

            empty_lines = [ln for _, ln in due_items(now, config=cfg)]  # no upcoming_fixtures
            if not any("t01_a_b" in ln or "t02_c_d" in ln for ln in empty_lines):
                fails.append(f"config path without upcoming_fixtures should still "
                             f"report on already-locked fixtures: {empty_lines}")
        finally:
            store_io.STORE_DIR = old_store

    return fails


def main():
    now = datetime.now(timezone.utc)
    print(f"=== wc2026 agenda @ {now.isoformat()[:16]}Z ===")
    for pri, line in due_items(now):
        print(f"  [P{pri}] {line}")


if __name__ == "__main__":
    sys.exit(main())
