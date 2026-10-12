"""The steps an operating session performs by hand, as commands.

Until 2026-10-06 locking a fixture, validating an evidence bundle, running J
and checking that a week was really finished were each a few lines of Python
that the operator re-typed from a runbook: team-name spelling, the goal-curve
key name, the order of validation and J, and "did I finish" all lived in the
operator's head. Each of those has gone wrong in this project at least once.
Here each is one command, and each refuses in the way the function underneath
refuses.

    ops lock --match-id <mid>
    ops evidence --match-id <mid> --claims <path>
    ops run-j --match-id <mid> --claims <path> [--j-model M] [--k 3]
    ops done --gameweek <n>
    ops preflight

NOTHING HERE PRINTS A FLAGGED CLAIM OR A PRICE. `evidence` reports indices,
fields and kinds of problem; `run-j` prints J's own probabilities (an
instrument reading, not a market number).
"""
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from football_ai.competitions import matchflow, schedule
from football_ai.store import io as store_io

OK, FAIL, LOSS, NA, UNCHECKED, PEND, WARN = ("ok", "FAIL", "loss", "n/a",
                                             "unchecked", "pending", "warn")
_MARK = {OK: "[OK]  ", FAIL: "[FAIL]", LOSS: "[LOSS]", NA: "[n/a] ",
         UNCHECKED: "[??]  ", PEND: "[WAIT]", WARN: "[WARN]"}


# --- lock ------------------------------------------------------------------

def _fixture(comp, match_id):
    for f in comp.get("upcoming_fixtures") or []:
        if f["match_id"] == match_id:
            return f
    raise ValueError(f"REFUSED: {match_id!r} is not in this competition's "
                     f"fixture list")


def lock_fixture(comp, match_id):
    """Lock M for one fixture, taking the team names and kickoff from the
    fixture list (never typed by hand) and refusing a team with no rating
    rather than locking it at the 1500 default."""
    fx = _fixture(comp, match_id)
    if not fx.get("kickoff_utc"):
        raise ValueError(
            f"REFUSED: {match_id!r} has no confirmed kickoff_utc. Confirm it "
            f"against the league's fixture pages and fill it in the fixture "
            f"file (see its HOW TO REFRESH); a guessed time is never recorded")
    ratings = comp["current_ratings"]()
    missing = [t for t in (fx["home_team"], fx["away_team"]) if t not in ratings]
    if missing:
        raise ValueError(
            f"REFUSED: no rating for {missing}. Locking would silently use "
            f"the default Elo of 1500 and freeze it; the name must match the "
            f"data source's spelling (see the fixture file's ROSTER)")
    return comp["lock_match"](match_id, fx["home_team"], fx["away_team"],
                              fx["kickoff_utc"], ratings=ratings)


def lock_summary(record):
    d = record["data"]
    p = d["payload"]
    return (f"locked {p['match_id'] if 'match_id' in p else record.get('match_id')} "
            f"M={p['probs_90']} kickoff={p['kickoff_utc']} sha256={d['sha256'][:12]}")


# --- evidence --------------------------------------------------------------

def load_claims(paths):
    """One bundle from one or several files, in the order given (the three
    scouts of a fixture each write one). Claim indices are positions in the
    merged list, which is the list `run_j` and the stored evidence carry."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    merged = []
    for path in paths:
        if _is_scout_report(path):  # a glob over the scout dir picks these up
            continue
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(c, dict) for c in data):
            raise ValueError(f"{path}: an evidence bundle is a JSON list of "
                             f"claim objects")
        merged.extend(data)
    return merged


SCOUT_REPORT_SUFFIX = ".scout.json"
PAGE_FETCH = ("ok", "partial", "blocked")
SCOUT_REPORT_FIELDS = ("market_content", "market_numbers_seen", "page_fetch",
                       "search_exhausted")


def scout_report_path(claims_path):
    """Where a scout writes its self-report: next to its claims file,
    `x.json` -> `x.scout.json`."""
    p = Path(claims_path)
    stem = p.name[:-len(".json")] if p.name.endswith(".json") else p.name
    return p.with_name(stem + SCOUT_REPORT_SUFFIX)


def _is_scout_report(path):
    return str(path).endswith(SCOUT_REPORT_SUFFIX)


def _withhold_report_text(text):
    """True when a self-report's free text must not be stored: any digit
    outside a bookmaker brand (bet365, 888sport) -- the brief asks for no
    figures at all -- or anything the firewall flags, including a price in
    words ("odds on", "evens") that has no digit. The store applies the
    firewall as its last guard, so the loader must withhold at least that
    much, or a report blocks the bundle it travels with (2026-10-09: "headline
    mentions of odds on several previews" passed the digit check and was
    refused at the store)."""
    from football_ai.evidence import firewall
    return (bool(re.search(r"\d", firewall._MARKET_WORDS_RE.sub(" ", text)))
            or bool(firewall.leaked_numbers(text)))


def load_scout_reports(paths):
    """Read each claims file's `.scout.json` self-report.

    WHY (2026-10-07). A scout's return value said whether it saw market
    numbers, could open pages, or ran out of searches -- and that reached
    only the orchestrating conversation. In GW06 five scouts saw market
    probabilities and every scout read search snippets only; none of it was
    in the ledger, so the judgment, and any later check of whether exposure
    biased a bundle, could not see it.

    A self-report NEVER blocks J: J's deadline is irreversible, and a report
    that blocked it would be deleted under pressure, losing the declaration.
    So a missing report is listed; a field that is missing or of the wrong
    type is kept as None and named in `invalid`; free text carrying any digit
    is withheld (None) rather than stored, so no market number reaches the
    ledger through it. Only the four declared fields are kept."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [p for p in paths if not _is_scout_report(p)]
    reports, missing = [], []
    for path in paths:
        rp, name = scout_report_path(path), Path(path).name
        if not rp.is_file():
            missing.append(name)
            continue
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if not isinstance(data, dict):
            reports.append({"file": name, **{k: None for k in SCOUT_REPORT_FIELDS},
                            "invalid": ["not a JSON object"]})
            continue
        rep, invalid = {"file": name}, []
        mc = data.get("market_content")
        if not isinstance(mc, str):
            rep["market_content"], invalid = None, invalid + ["market_content"]
        elif _withhold_report_text(mc):
            rep["market_content"] = None
            invalid.append("market_content (contained figures; withheld)")
        else:
            rep["market_content"] = mc
        for k in ("market_numbers_seen", "search_exhausted"):
            ok = isinstance(data.get(k), bool)
            rep[k] = data.get(k) if ok else None
            invalid += [] if ok else [k]
        ok = data.get("page_fetch") in PAGE_FETCH
        rep["page_fetch"] = data.get("page_fetch") if ok else None
        invalid += [] if ok else ["page_fetch"]
        rep["invalid"] = invalid
        reports.append(rep)
    return {"files": len(paths), "reports": reports, "missing": missing}


def check_evidence(config, claims, scout=None):
    """Validate a bundle and measure its coverage, WITHOUT showing any
    flagged text. Returns a dict; `ok` means it may go to run-j. `scout` is
    `load_scout_reports(...)`, shown alongside; it never changes the
    verdict (see load_scout_reports)."""
    from football_ai.evidence import schema
    bundle = schema.validate_bundle(claims, categories=list(config.evidence_categories),
                                    require_dimension=True)
    cov = schema.coverage(claims)
    silent = list(cov["silent"])
    problems = schema.refusal_text(bundle) if (bundle["errors"] or bundle["leaks"]) else ""
    return {"claims": len(claims), "problems": problems, "silent": silent,
            "covered": list(cov["covered"]), "scout": scout,
            "ok": not problems and not silent}


def render_scout_reports(sc):
    names = lambda xs: ", ".join(xs) or "none"  # noqa: E731
    reps = sc["reports"]
    line = (f"scout reports: {len(reps)}/{sc['files']}"
            + (f" (missing: {names(sc['missing'])})" if sc["missing"] else "")
            + " | saw market numbers: "
            + names([r["file"] for r in reps if r["market_numbers_seen"]])
            + f" | pages blocked: {sum(r['page_fetch'] == 'blocked' for r in reps)}"
            + f" | searches exhausted: {sum(r['search_exhausted'] is True for r in reps)}")
    out = [line]
    bad = [f"{r['file']}: {', '.join(r['invalid'])}" for r in reps if r["invalid"]]
    if bad:
        out.append("scout report fields recorded as unknown -- " + "; ".join(bad))
    if sc["missing"] or bad:
        out.append("-> ask the scouts to write or fix their reports (J is not held "
                   "for this; the gap is recorded as it stands)")
    if reps and all(r["page_fetch"] == "blocked" for r in reps):
        out.append("-> every scout read search snippets only: expect J to stay at M "
                   "(see `ops preflight`, evidence sources)")
    return out


def render_evidence_check(res):
    lines = [f"claims: {res['claims']} | dimensions covered: "
             f"{len(res['covered'])}/{len(res['covered']) + len(res['silent'])}"]
    lines.append("validation: " + (res["problems"] or "passed (schema and firewall)"))
    lines.append("silent dimensions: " + (", ".join(res["silent"]) or "none"))
    if res.get("scout") is not None:
        lines.extend(render_scout_reports(res["scout"]))
    if res["problems"]:
        lines.append("-> send the listed claims back to the scout to be rewritten "
                     "without the market number; do not look for the flagged "
                     "text and do not bypass the validator")
    if res["silent"]:
        lines.append("-> dispatch more evidence gathering for the silent "
                     "dimensions (an honest negative finding with its sources "
                     "counts as coverage); do not run the judgment on it")
    lines.append("RESULT: " + ("OK" if res["ok"] else
                              "REJECTED (validation failed; do not read or use the "
                              "flagged claims)" if res["problems"] else
                              "INCOMPLETE (validation passed; silent dimensions "
                              "remain -- the bundle may still be stored and used "
                              "for J)"))
    return lines


def evidence_exit_code(res):
    """0 ready; 1 rejected (nothing may go forward); 2 valid but incomplete
    (J may run on it, the judgment may not). Exit 1 for both used to read as
    'hold the bundle', and a held bundle past the J deadline is a lost J."""
    return 1 if res["problems"] else (2 if res["silent"] else 0)


# --- generated briefs ------------------------------------------------------
# The scout prompt and the judgment workflow's arguments used to be typed by
# hand from a runbook. Typed prices were wrong in three of the eight judgment
# records this ledger holds (`brief_price_error`), and a hand-copied list of
# dimensions goes stale the day `schema.DIMENSIONS` changes. Both are now
# built from the code that defines them.

TEAM_DIMENSIONS = ("availability", "squad_composition", "press_style",
                   "set_pieces", "phase_xg", "regime")
MATCH_DIMENSIONS = ("rest_travel", "referee", "venue_conditions", "incentive")

# Sites whose pages are mostly prices, tips or prediction markets. WebSearch
# summaries built from them put market numbers in front of the scout: five
# of GW06's thirty scouts reported exactly that (2026-10-07). Passing them as
# `blocked_domains` lowers that exposure; it cannot remove it, because
# ordinary previews quote prices too -- hence the self-report below. Sites
# that are also real stats or news sources (Squawka, RotoWire, TNT Sports)
# are deliberately not listed: blocking them would cost evidence.
SCOUT_BLOCKED_DOMAINS = (
    "oddschecker.com", "oddsportal.com", "oddscalendar.com", "oddslot.com",
    "forebet.com", "dimers.com", "picksandparlays.net", "og.com",
    "manifold.markets", "polymarket.com", "kalshi.com", "gambling.com",
    "thefootballfaithful.com", "footballwhispers.com", "vegasodds.com",
    "actionnetwork.com", "onsidearena.com", "ats.io", "bet365.com",
    "betmgm.com", "williamhill.com", "paddypower.com", "skybet.com",
    "betfair.com",
)


def scout_prompt(config, fixture, side, out_path):
    """The prompt for one price-blind scout. Three scouts cover a fixture:
    home team, away team, and the match itself (four of the ten dimensions
    belong to neither team, so two scouts would leave them silent)."""
    from football_ai.evidence import schema
    if side not in ("home", "away", "match"):
        raise ValueError("side must be home, away or match")
    home, away = fixture["home_team"], fixture["away_team"]
    if side == "match":
        scope = f"比賽層級(不屬於任一隊):{home} 對 {away}"
        dims = MATCH_DIMENSIONS
    else:
        team = home if side == "home" else away
        scope = f"{team}(本場為{'主' if side == 'home' else '客'}隊,對手 {away if side == 'home' else home})"
        dims = TEAM_DIMENSIONS
    report_path = scout_report_path(out_path)
    return f"""你是**價格盲**蒐證者。絕不搜尋、閱讀、引用任何賠率、盤口、隱含機率、博彩預測或任何來自博彩市場的數字。分析性數字(xG、PPDA、分鐘數、傷停日期、Elo)是要的。若頁面含賠率或投注元件,完全忽略,不得帶進輸出。

搜尋:每次使用 WebSearch 都傳入 `blocked_domains`={list(SCOUT_BLOCKED_DOMAINS)}。這些站以賠率、明牌或預測市場為主,搜尋摘要會把數字帶進你的視野。

任務:為英超 {fixture['match_id']}({home} 對 {away},開球 {fixture.get('kickoff_utc') or fixture.get('date')})蒐集證據。你負責:{scope}。

你負責的維度(每一個都要有交代):{', '.join(dims)}
全部維度的定義:{'; '.join(schema.DIMENSIONS)}。查不到就交一筆負面發現:category `other`、source_tier `3`、claim 寫「這個指標找不到公開來源,我查了 A、B、C」。負面發現是有價值的產出,沉默才是不合格。

輸出:一個 JSON list,寫入檔案 {out_path}(該路徑在 repo 之外;抓網頁用 `curl -o <絕對路徑>`,不得在 repo 內留下任何檔案)。每筆 claim 的欄位:
- 必填:`claim`(一句事實)、`category`(從 {list(config.evidence_categories)} 擇一)、`source`(來源名稱,不得含賠率)、`source_tier`(從 {schema.TIERS} 擇一,1a=官方第一手,1b=現場媒體,2=二手分析,3=低可信)、`access_time`(ISO 含時區)、`absorption`(從 {schema.ABSORPTION} 擇一,依資訊有多公開判斷,絕不看價格)、`absorption_reason`、`dimension`(上列維度之一,漏標會被驗證器擋下)。
- 選填:`direction`(`home`、`away` 或 `none`,這筆資訊對誰有利)、`magnitude_prior`(量級的粗略描述,字串,不得含市場數字)、`pub_time`(ISO)。

另寫一個 JSON object 到 {report_path}(你的自報狀態,會隨證據入帳;不得含任何數字):`market_content`(規則同下方回傳值)、`market_numbers_seen`(true 或 false:你的工具輸出裡是否出現過本場的賠率、盤口、隱含機率或預測市場數字,不論是否寫入)、`page_fetch`(`ok`、`partial` 或 `blocked`:能否開啟原始網頁)、`search_exhausted`(true 或 false:搜尋額度是否中途用盡)。

回傳值只有這些結構化欄位,不得摘要、不得貼上 claim 內容(未驗證文字不進分析者視野):`path`、`claims`(數量)、`dimensions`(已覆蓋的維度)、`negative_findings`(數量)、`market_content`(`none`,或只描述看到何種類型、出自哪個站,不得寫入數字本身)、`sources_used`(僅列來源機構名)。"""


def judgment_brief(comp, gameweek, now=None, build_fn=None, out_dir=None):
    """The `args` for the judgment-synthesis workflow, assembled from the
    ledger and the report -- no typed number. Returns {"args": {...},
    "skipped": [{match_id, reason}]}: a fixture that cannot be judged yet (no
    stored evidence, a silent dimension) is listed, never dropped quietly."""
    import tempfile
    from football_ai.competitions import ops
    from football_ai.core.paths import repo_root
    config = comp["config"]
    now = now or datetime.now(timezone.utc)
    if build_fn is None:
        from football_ai.decision import report
        build_fn = lambda mid: report.build(mid, mechanism=None, config=config)
    out = Path(out_dir or Path(tempfile.gettempdir()) / "football_ai_brief")
    out.mkdir(parents=True, exist_ok=True)
    matches, skipped = [], []
    for fx in comp.get("upcoming_fixtures") or []:
        if fx.get("gameweek") != gameweek:
            continue
        mid = fx["match_id"]
        ph = matchflow.phase(config, mid, now=now, kickoff_utc=fx.get("kickoff_utc"))
        if ph in ("UNLOCKED", "KICKED_OFF", "SETTLED", "JUDGED"):
            skipped.append({"match_id": mid, "reason": f"phase {ph}: nothing to judge"})
            continue
        if ph in ("M_LOCKED", "J_DEGRADED"):
            skipped.append({"match_id": mid, "reason": f"phase {ph}: a J is still owed; "
                                                       "finish that before any judgment"})
            continue
        if ph == "J_LOCKED":
            skipped.append({"match_id": mid, "reason": "phase J_LOCKED: waiting for the "
                            "scheduled price; the brief needs the market block"})
            continue
        ev_rows = [r for r in store_io.read("evidence", era=config.era)
                   if r.get("match_id") == mid]
        if not ev_rows:
            skipped.append({"match_id": mid, "reason": "no stored evidence bundle "
                            "(`ops evidence --store`)"})
            continue
        ev = ev_rows[-1]["data"]
        cov = ev.get("coverage") or {}
        if cov.get("silent"):
            skipped.append({"match_id": mid, "reason": "evidence leaves silent "
                            f"dimensions: {', '.join(cov['silent'])}"})
            continue
        locks = sorted([r for r in store_io.read("locks", era=config.era)
                        if r.get("match_id") == mid], key=lambda r: r.get("version", 1))
        base = locks[0]["data"]["payload"]
        j = next((l["data"]["payload"].get("j") for l in reversed(locks)
                  if l["data"]["payload"].get("j")), None)
        bundle_file = out / f"{mid}_evidence.json"
        bundle_file.write_text(json.dumps(ev["claims"], ensure_ascii=False, indent=1),
                               encoding="utf-8")
        market = None
        if ph in ("PRICED", "J_FORECLOSED"):
            b = build_fn(mid)
            one = next((m for m in b["markets"] if m.get("market") == "1x2"), None)
            if one is not None:
                fair = one["market_fair"]
                m_probs = base["probs_90"]
                market = {
                    "devigged": {k: fair[k] for k in ("home", "draw", "away")},
                    "best_prices": [{"selection": sel,
                                     "book": (one.get("best_book") or {}).get(sel, one.get("book")),
                                     "decimal_odds": one["odds"][sel]}
                                    for sel in ("home", "draw", "away")],
                    "gap_pp": round(max(abs(m_probs[k] - fair[k]) for k in ("home", "draw", "away")) * 100, 2),
                }
        j_ok = j is not None and ((j.get("runner") or {}).get("k_valid") is None
                                   or j["runner"]["k_valid"] >= ops.MIN_K_FOR_PRICING)
        # No estimate independent of M exists when J is missing or moved
        # nothing off M (zero factors, as in all ten GW06 fixtures). The
        # workflow then runs from-zero forensics, which builds one without
        # reading M or the market (owner, 2026-10-10).
        no_independent = not j_ok or not (j or {}).get("factors")
        matches.append({
            "mid": mid, "home": fx["home_team"], "away": fx["away_team"],
            "kickoff": base["kickoff_utc"], "bundle_file": str(bundle_file),
            "coverage": cov, "scout_reports": ev.get("scout_reports"),
            "m_probs": base["probs_90"],
            "j_probs": (j or {}).get("probs_90") if j_ok else None,
            "j_status": "locked" if j_ok else ("foreclosed" if ph == "J_FORECLOSED" else "absent"),
            "j_factors": (j or {}).get("factors") or [], "j_basis": (j or {}).get("basis") or [],
            "market": market, "live_divergence": no_independent,
            "divergence_reason": ("no estimate independent of M: J is "
                                  + ("missing" if not j_ok else "locked with zero factors (J = M)")
                                  if no_independent else "")})
    args = {"repo_root": str(repo_root()), "era": config.era,
            "today": now.date().isoformat(),
            "known_biases": list(config.known_biases),
            "depth": {"escalate_pp": config.gate.watch_pp}, "matches": matches}
    return {"args": args, "skipped": skipped}


# --- done ------------------------------------------------------------------

def _latest(rows):
    return rows[-1] if rows else None


def _ledger_state(root=None):
    """('clean'|'dirty'|'unpushed'|'unchecked', detail) for the ledger files."""
    from football_ai.store import sync
    root = Path(root or sync.ROOT)
    try:
        st = sync._git(["status", "--porcelain", "--", sync.STORE_PATH], root)
        if st.returncode != 0:
            return "unchecked", st.stderr.strip()[:120]
        if st.stdout.strip():
            return "dirty", "uncommitted ledger changes"
        lg = sync._git(["log", f"origin/{sync.LEDGER_BRANCH}..HEAD", "--oneline",
                        "--", sync.STORE_PATH], root)
        if lg.returncode != 0:
            return "unchecked", lg.stderr.strip()[:120]
        if lg.stdout.strip():
            return "unpushed", f"{len(lg.stdout.splitlines())} local commit(s) touching the ledger"
        return "clean", ""
    except Exception as e:  # noqa: BLE001 - a check that cannot run says so
        return "unchecked", str(e)[:120]


def done_rows(comp, gameweek, now=None):
    """One row per fixture of the week: its checks, each OK / FAIL / LOSS /
    PEND / n/a. A LOSS is a permanent, recorded gap (never locked, J
    foreclosed); it is reported, not failed, because nothing can be done about
    it. PEND is work that cannot start yet (the judgment waits for the
    scheduled price): the week is not done, but nothing is owed right now."""
    from football_ai.competitions import ops
    from football_ai.decision import judgment
    config = comp["config"]
    now = now or datetime.now(timezone.utc)
    rows = []
    for fx in comp.get("upcoming_fixtures") or []:
        if fx.get("gameweek") != gameweek:
            continue
        mid = fx["match_id"]
        ph = matchflow.phase(config, mid, now=now, kickoff_utc=fx.get("kickoff_utc"))
        checks = []
        ko = matchflow._parse(fx.get("kickoff_utc"))

        if ph == "UNLOCKED":
            gone = (ko is not None and ko < now) or (
                ko is None and (fx.get("date") or "9999") < now.date().isoformat())
            checks.append(("lock", LOSS if gone else FAIL,
                           "kickoff passed with no lock" if gone else "M not locked"))
            rows.append({"match_id": mid, "fixture": f"{fx['home_team']} v {fx['away_team']}",
                         "phase": ph, "checks": checks})
            continue

        locks = sorted([r for r in store_io.read("locks", era=config.era)
                        if r.get("match_id") == mid], key=lambda r: r.get("version", 1))
        j_block = next((l["data"]["payload"].get("j") for l in reversed(locks)
                        if l["data"]["payload"].get("j")), None)
        priced = any(r.get("match_id") == mid for r in store_io.read("odds", era=config.era))
        checks.append(("lock", OK, "M locked"))
        k_valid = ((j_block or {}).get("runner") or {}).get("k_valid") if j_block else None
        if j_block and (k_valid is None or k_valid >= ops.MIN_K_FOR_PRICING):
            checks.append(("J", OK, f"k_valid={k_valid}"))
            j_state = OK
        elif j_block:
            checks.append(("J", LOSS if priced else FAIL,
                           f"degraded (k_valid={k_valid}); "
                           + ("priced, cannot be re-run" if priced else "re-run before the deadline")))
            j_state = LOSS if priced else FAIL
        elif priced or (ko is not None and ko < now):
            checks.append(("J", LOSS, "no J and none can be formed now (priced or kicked off)"))
            j_state = LOSS
        else:
            dl = schedule.j_deadline(ko) if ko else None
            checks.append(("J", FAIL, "J owed" + (f"; deadline {dl:%m-%d %H:%MZ}" if dl else "")))
            j_state = FAIL

        jd_rows = [r for r in store_io.read("judgments", era=config.era)
                   if r["data"].get("match_id") == mid]
        jd = _latest(jd_rows)
        ev_rows = [r for r in store_io.read("evidence", era=config.era)
                   if r.get("match_id") == mid]
        ev = _latest(ev_rows)
        silent = list(((ev or {}).get("data") or {}).get("coverage", {}).get("silent", [])) if ev else None
        waiver = (jd or {}).get("data", {}).get("coverage_waiver") if jd else None
        # once the match has kicked off, evidence and a lean can no longer be
        # produced as of before kickoff: a gap there is a LOSS, like a lock
        # that was never made, not something to fix
        gone = ph in ("KICKED_OFF", "SETTLED")
        late = LOSS if gone else FAIL
        if ev is not None and not silent:
            checks.append(("evidence", OK, "stored, all dimensions covered"))
        elif waiver:
            checks.append(("evidence", OK, "waived (declared in the judgment): " + str(waiver)[:60]))
        elif ev is None:
            checks.append(("evidence", late, "no stored evidence bundle for this fixture"
                           + (" (kickoff passed)" if gone else "")))
        else:
            checks.append(("evidence", late, f"silent dimensions: {', '.join(silent)}"
                           + (" (kickoff passed)" if gone else "")))

        if jd is None:
            if ph == "J_LOCKED":
                checks.append(("judgment", PEND, "waiting for the scheduled price"))
            elif gone:
                checks.append(("judgment", LOSS, "no judgment record and kickoff "
                               "has passed: a lean written now is not a prediction"))
            else:
                checks.append(("judgment", FAIL, "no judgment record"))
        else:
            d = jd["data"]
            errs = judgment.validate_record(d)
            if errs:
                checks.append(("judgment", FAIL, f"{len(errs)} validation error(s): "
                               + errs[0][:80]))
            else:
                checks.append(("judgment", OK, f"direction={d.get('direction')} "
                               f"model={d.get('model', 'undeclared')}"))
        rows.append({"match_id": mid, "fixture": f"{fx['home_team']} v {fx['away_team']}",
                     "phase": ph, "checks": checks})
    return rows


def render_done(rows, ledger):
    """Lines and the number of open items. An open item is a FAIL or a PEND:
    a week with work still waiting on the price is not done either."""
    lines, failed, waiting = [], 0, 0
    for r in rows:
        lines.append(f"{r['match_id']}  {r['fixture']}  [{r['phase']}]")
        for name, status, note in r["checks"]:
            lines.append(f"    {_MARK[status]} {name:9} {note}")
            failed += status == FAIL
            waiting += status == PEND
    state, detail = ledger
    status = OK if state == "clean" else (UNCHECKED if state == "unchecked" else FAIL)
    lines.append(f"ledger: {_MARK[status]} {state}{(' - ' + detail) if detail else ''}")
    failed += status == FAIL
    open_ = failed + waiting
    lines.append("RESULT: " + ("DONE" if not open_ else
                               f"NOT DONE ({failed} to fix, {waiting} waiting for the price)"))
    return lines, open_


# --- preflight -------------------------------------------------------------

def _sync_check():
    from football_ai.store import sync
    try:
        return OK, str(sync.assert_current())
    except Exception as e:  # noqa: BLE001
        return FAIL, str(e)[:240]


def _claude_probe(timeout=120):
    from football_ai.research import j_runner
    exe = shutil.which("claude")
    if not exe:
        return FAIL, "the `claude` executable is not on PATH: J cannot run, and a locked M with no J is permanent"
    try:
        out = j_runner.cli_llm("Reply with exactly one word: PONG", timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return FAIL, f"claude -p failed: {str(e)[:200]}"
    if "PONG" not in str(out):
        return FAIL, f"claude -p answered unexpectedly: {str(out)[:60]!r}"
    models = ", ".join(getattr(out, "models", ()) or ()) or "model not reported"
    return OK, f"isolated claude -p answers ({models})"


def _gh(args, timeout=20):
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=timeout)
    except Exception as e:  # noqa: BLE001 - gh missing, offline, blocked
        return None


def _repo_slug():
    from football_ai.store import sync
    r = sync._git(["remote", "get-url", "origin"], sync.ROOT)
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().rstrip("/").removesuffix(".git").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def _odds_key_check():
    slug = _repo_slug()
    r = _gh(["secret", "list", "--repo", slug]) if slug else None
    if r is None or r.returncode != 0:
        return UNCHECKED, ("cannot be verified from this environment (no gh, or "
                           "the proxy refuses the secrets API); the owner must "
                           "confirm ODDS_API_KEY is set")
    names = {line.split()[0] for line in r.stdout.splitlines() if line.strip()}
    return (OK, "ODDS_API_KEY is set") if "ODDS_API_KEY" in names else (
        FAIL, "ODDS_API_KEY is NOT set: a locked fixture would get no closing price")


def _actions_health():
    slug = _repo_slug()
    r = _gh(["run", "list", "--repo", slug, "--workflow", "epl-matchday.yml",
             "--limit", "3", "--json", "conclusion,createdAt"]) if slug else None
    if r is None or r.returncode != 0:
        return UNCHECKED, "cannot be verified from this environment"
    try:
        runs = json.loads(r.stdout)
    except ValueError:
        return UNCHECKED, "unreadable gh output"
    if not runs:
        return UNCHECKED, "no runs recorded"
    bad = [x for x in runs if x.get("conclusion") not in ("success", None, "")]
    return (FAIL, f"{len(bad)} of the last {len(runs)} epl-matchday runs did not succeed") if bad else (
        OK, f"last {len(runs)} epl-matchday runs succeeded")


# Pages a scout must be able to open for J to have anything to work with:
# player output and xG (stats) and team news (news). Probed through the same
# egress the scouts use.
EVIDENCE_HOSTS = {"stats": ("fbref.com", "understat.com"),
                  "news": ("www.premierleague.com", "www.bbc.co.uk",
                           "www.skysports.com")}


def _probe_host(host, timeout=8, opener=None):
    """'reachable' if the site itself answered (any HTTP status: a 403 from
    the SITE still means egress is open), 'blocked' if the egress proxy
    refused the tunnel, 'unreachable' for anything else (offline, DNS,
    timeout) -- which is not evidence of a policy block."""
    import urllib.error
    import urllib.request
    opener = opener or urllib.request.urlopen
    try:
        opener(urllib.request.Request(f"https://{host}/", method="HEAD"),
               timeout=timeout)
        return "reachable"
    except urllib.error.HTTPError:  # before URLError: it is a subclass
        return "reachable"
    except urllib.error.URLError as e:
        msg = str(e.reason)
        if "Tunnel connection failed" in msg and ("403" in msg or "407" in msg):
            return "blocked"
        return "unreachable"
    except Exception:  # noqa: BLE001
        return "unreachable"


def _evidence_sources_check(probe=None):
    """Can scouts open pages at all?

    WHY (2026-10-07). In this cloud environment every page fetch was refused
    by the network policy, so GW06's thirty scouts built their bundles from
    search-result summaries alone: tier 1a almost vanished, player output
    was "not found", and J -- the same rr1 spec that took factors on 9 of 9
    MW3 fixtures -- took none on 10 of 10 and returned M unchanged. Every
    other check said GO. A whole group (stats or news) with nothing
    reachable and at least one host refused by policy is a WARN, not a
    FAIL: locking M is still right (preflight's NO-GO means "do not lock"),
    and J should still run -- but the gap must be reported. Nothing
    reachable for other reasons is unchecked."""
    probe = probe or _probe_host
    state = {h: probe(h) for hosts in EVIDENCE_HOSTS.values() for h in hosts}
    blocked = [h for h, s in state.items() if s == "blocked"]
    dead = [g for g, hosts in EVIDENCE_HOSTS.items()
            if not any(state[h] == "reachable" for h in hosts)]
    if any(state[h] == "blocked" for g in dead for h in EVIDENCE_HOSTS[g]):
        return WARN, (
            f"the network policy refuses page fetches ({', '.join(blocked)}): "
            f"scouts can read search summaries only, and J on such a bundle "
            f"stays at M (GW06: no factor on 10 of 10 fixtures). The owner adds "
            f"these hosts under the cloud environment's Network access. If that "
            f"cannot happen before a J deadline, still run J and report the gap: "
            f"a thin J is recorded, an absent one is lost")
    if dead:
        return UNCHECKED, (f"no {' or '.join(dead)} source answered "
                           f"({', '.join(h for g in dead for h in EVIDENCE_HOSTS[g])}); "
                           f"offline or down, not a policy block")
    return OK, ("the network allows page fetches (a site may still refuse "
                "automated requests)"
                + (f"; still blocked: {', '.join(blocked)}" if blocked else ""))


def preflight(comp, probe=True, now=None, days=14):
    """GO / NO-GO before any M is locked. What cannot be checked from here is
    reported as unchecked, never as a pass."""
    now = now or datetime.now(timezone.utc)
    items = [("origin sync", *_sync_check())]
    items.append(("J subprocess", *(_claude_probe() if probe else (UNCHECKED, "skipped (--no-probe)"))))
    items.append(("evidence sources", *_evidence_sources_check()))
    items.append(("ODDS_API_KEY secret", *_odds_key_check()))
    items.append(("scheduled job health", *_actions_health()))
    horizon = now.timestamp() + days * 86400
    unconfirmed = []
    for fx in comp.get("upcoming_fixtures") or []:
        if fx.get("kickoff_utc"):
            continue
        d = fx.get("date")
        if d and now.date().isoformat() <= d <= datetime.fromtimestamp(horizon, timezone.utc).date().isoformat():
            unconfirmed.append(fx["match_id"])
    items.append(("kickoff times", *((FAIL, f"{len(unconfirmed)} fixture(s) in the next "
                                      f"{days} days have no confirmed kickoff_utc, e.g. {unconfirmed[0]}")
                                     if unconfirmed else (OK, f"confirmed for every fixture in the next {days} days"))))
    pause = comp.get("paused")
    items.append(("competition status", *((UNCHECKED, f"PAUSED since {pause['since']}: {pause['reason']}")
                                          if pause else (OK, "active"))))
    return items


def render_preflight(items):
    lines = [f"{_MARK[s]} {name:22} {detail}" for name, s, detail in items]
    failed = sum(1 for _, s, _ in items if s == FAIL)
    unchecked = sum(1 for _, s, _ in items if s == UNCHECKED)
    warned = [name for name, s, _ in items if s == WARN]
    lines.append("RESULT: " + ("GO" if not failed else f"NO-GO ({failed} failing)")
                 + (f"; WARNING: {', '.join(warned)} (report it in this round's "
                    f"output)" if warned else "")
                 + (f"; {unchecked} item(s) could not be checked from here" if unchecked else ""))
    return lines, failed


# --- command line ----------------------------------------------------------

def cli(args, comp):
    """Dispatch for ops lock / evidence / run-j / done / preflight. Returns an
    exit code."""
    from football_ai.competitions import ops
    if args.action == "lock":
        _need(args, "--match-id")
        print(lock_summary(lock_fixture(comp, args.match_id)))
        print("next: gather price-blind evidence, then `ops evidence` and `ops run-j` "
              "in THIS session, before the J deadline (`python -m football_ai.competitions.matchflow`)")
        return 0
    if args.action == "evidence":
        _need(args, "--claims")
        claims = load_claims(args.claims)
        scout = load_scout_reports(args.claims)
        res = check_evidence(comp["config"], claims, scout=scout)
        lines = render_evidence_check(res)
        print("\n".join(lines))
        if args.store:
            _need(args, "--match-id")
            if res["problems"]:
                print("not stored: the bundle did not pass validation")
                return 1
            ev = ops.store_evidence(comp["config"], args.match_id, claims,
                                    scout_reports=scout)
            print("stored" if ev["evidence_stored"] else "already stored (same content)",
                  f"sha256={ev['claims_sha256'][:12]}",
                  "-- coverage incomplete: judgment.record will refuse without a "
                  "coverage_waiver" if res["silent"] else "")
        return evidence_exit_code(res)
    if args.action == "run-j":
        _need(args, "--match-id")
        _need(args, "--claims")
        out = ops.run_j(comp["config"], args.match_id, load_claims(args.claims),
                        comp["amend_lock"], goal_curve=comp["goal_curve"],
                        k=args.k, j_model=args.j_model,
                        scout_reports=load_scout_reports(args.claims))
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print(f"k_valid={out['k_valid']}: below {ops.MIN_K_FOR_PRICING} the scheduled "
              f"job treats this J as absent; re-run before the deadline." if
              out["k_valid"] < ops.MIN_K_FOR_PRICING else
              "next: `python -m football_ai.store.sync publish -m \"...\"` NOW "
              "(the J counts only once it is on origin), then wait for the price")
        return 0
    if args.action == "evidence-brief":
        _need(args, "--match-id")
        _need(args, "--side")
        _need(args, "--scout-writes-to")
        print(scout_prompt(comp["config"], _fixture(comp, args.match_id), args.side,
                           args.scout_writes_to))
        return 0
    if args.action == "judgment-brief":
        _need(args, "--gameweek")
        res = judgment_brief(comp, args.gameweek)
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        return 0
    if args.action == "done":
        _need(args, "--gameweek")
        lines, failed = render_done(done_rows(comp, args.gameweek), _ledger_state())
        print("\n".join(lines))
        return 1 if failed else 0
    if args.action == "preflight":
        lines, failed = render_preflight(preflight(comp, probe=not args.no_probe))
        print("\n".join(lines))
        return 1 if failed else 0
    if args.action == "summary":
        _need(args, "--gameweek")
        from football_ai.competitions import summary
        path, data = summary.write(comp, args.gameweek, out=args.out)
        print(summary.text_summary(data))
        print(f"page: {path}")
        return 0
    raise ValueError(args.action)


def _need(args, flag):
    attr = flag.lstrip("-").replace("-", "_")
    if getattr(args, attr, None) in (None, ""):
        raise SystemExit(f"{args.action} needs {flag}")


def _selfcheck():
    import contextlib
    import io
    import tempfile
    from types import SimpleNamespace

    from football_ai.competitions import ops
    from football_ai.competitions.epl_2026_27 import CONFIG

    fails = []
    KO = "2026-10-10T11:30:00+00:00"
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    fixtures = [{"match_id": "t_a_b", "gameweek": 6, "home_team": "A", "away_team": "B",
                 "date": "2026-10-10", "kickoff_utc": KO},
                {"match_id": "t_c_d", "gameweek": 6, "home_team": "C", "away_team": "D",
                 "date": "2026-10-10", "kickoff_utc": None},
                {"match_id": "t_gone", "gameweek": 6, "home_team": "E", "away_team": "F",
                 "date": "2026-09-01", "kickoff_utc": "2026-09-01T11:30:00+00:00"}]
    locked = []

    def fake_lock(mid, h, a, ko, ratings=None):
        locked.append((mid, h, a, ko, sorted(ratings)))
        return {"match_id": mid, "data": {"sha256": "0" * 64, "payload": {
            "match_id": mid, "kickoff_utc": ko, "probs_90": {"home": .5, "draw": .25, "away": .25}}}}

    comp = {"config": CONFIG, "upcoming_fixtures": fixtures,
            "current_ratings": lambda: {"A": 1500.0, "B": 1500.0, "C": 1500.0},
            "lock_match": fake_lock}

    # lock: names and kickoff come from the fixture list; a missing rating or
    # kickoff is refused rather than defaulted
    lock_fixture(comp, "t_a_b")
    if locked != [("t_a_b", "A", "B", KO, ["A", "B", "C"])]:
        fails.append(f"lock_fixture must pass the fixture's own names/kickoff: {locked}")
    for mid, why in (("t_c_d", "no confirmed kickoff"), ("t_gone", "no rating"),
                     ("nope", "not in the fixture list")):
        try:
            lock_fixture(comp, mid)
            fails.append(f"lock_fixture must refuse {mid} ({why})")
        except ValueError:
            pass

    # several scout files merge in order into one bundle
    with tempfile.TemporaryDirectory() as td0:
        pa, pb = Path(td0) / "a.json", Path(td0) / "b.json"
        pa.write_text(json.dumps([{"claim": "x"}]), encoding="utf-8")
        pb.write_text(json.dumps([{"claim": "y"}, {"claim": "z"}]), encoding="utf-8")
        if [c["claim"] for c in load_claims([pa, pb])] != ["x", "y", "z"]:
            fails.append("load_claims must merge several files in the order given")
        pb.write_text(json.dumps({"claim": "not a list"}), encoding="utf-8")
        try:
            load_claims([pa, pb])
            fails.append("a bundle file that is not a list of claims must be refused")
        except ValueError:
            pass

    # evidence check: coverage and redaction
    good = {"claim": "Saka trained fully on Thursday", "category": "lineup",
            "source": "club site", "source_tier": "1a",
            "access_time": "2026-10-06T10:00:00+00:00", "absorption": "likely_unpriced",
            "absorption_reason": "not widely reported", "dimension": "availability"}
    res = check_evidence(CONFIG, [good])
    if res["ok"] or "press_style" not in res["silent"]:
        fails.append("one claim cannot cover ten dimensions; silent dimensions must block")
    leaky = dict(good, claim="Pinnacle opened Arsenal at 2.10 3.30 3.80")
    res = check_evidence(CONFIG, [leaky])
    text = "\n".join(render_evidence_check(res))
    if any(tok in text for tok in ("2.10", "3.30", "Pinnacle")) or "market-derived" not in text:
        fails.append(f"the evidence report must name the problem and show none of it: {text!r}")

    # exit codes: a valid-but-incomplete bundle must not read as "rejected"
    # (it may still go to J; holding it past the deadline loses the J)
    ready = {"claims": 1, "problems": "", "silent": [], "covered": ["a"], "ok": True}
    incomplete = dict(ready, silent=["referee"], ok=False)
    rejected = dict(ready, problems="claim 0: market-derived", ok=False)
    if (evidence_exit_code(ready), evidence_exit_code(incomplete),
            evidence_exit_code(rejected)) != (0, 2, 1):
        fails.append("evidence exit codes must be 0 ready / 2 incomplete / 1 rejected")
    if "INCOMPLETE" not in render_evidence_check(incomplete)[-1] \
            or "REJECTED" not in render_evidence_check(rejected)[-1] \
            or "OK" not in render_evidence_check(ready)[-1]:
        fails.append("the RESULT line must say which of the three it is")

    # done: every check present, losses reported not failed, a missing
    # judgment fails
    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            store_io.append("locks", {"version": 1, "payload": {
                "match_id": "t_a_b", "kickoff_utc": KO, "probs_90": {"home": .4, "draw": .3, "away": .3}}},
                era=CONFIG.era, match_id="t_a_b", version=1)
            rows = done_rows(comp, 6, now=now)
            by = {r["match_id"]: dict((c[0], c[1]) for c in r["checks"]) for r in rows}
            if by["t_a_b"].get("J") != FAIL or by["t_a_b"].get("judgment") != FAIL:
                fails.append(f"a locked fixture with no J and no judgment must fail both: {by['t_a_b']}")
            if by["t_gone"].get("lock") != LOSS:
                fails.append("a fixture that kicked off unlocked is a recorded LOSS, not a failure")
            if by["t_c_d"].get("lock") != FAIL:
                fails.append("an unlocked, still-lockable fixture must fail the lock check")
            lines, failed = render_done(rows, ("clean", ""))
            if not failed or "NOT DONE" not in lines[-1]:
                fails.append("render_done must report open items")
            _, failed_dirty = render_done([], ("unpushed", "2 commits"))
            if not failed_dirty:
                fails.append("unpushed ledger changes must count as not done")
            _, failed_unk = render_done([], ("unchecked", "no git"))
            if failed_unk:
                fails.append("an unverifiable ledger state is reported, not failed")
            # a locked J with no price yet is waiting, not failing; once priced,
            # a missing judgment is a failure
            jgood = {"probs_90": {"home": .4, "draw": .3, "away": .3},
                     "factors": [], "runner": {"k_valid": 3}}
            store_io.append("locks", {"version": 2, "payload": {
                "match_id": "t_a_b", "kickoff_utc": KO, "probs_90": {"home": .4, "draw": .3, "away": .3},
                "j": jgood}}, era=CONFIG.era, match_id="t_a_b", version=2)
            rows = done_rows(comp, 6, now=now)
            by = {r["match_id"]: dict((c[0], c[1]) for c in r["checks"]) for r in rows}
            if by["t_a_b"].get("judgment") != PEND:
                fails.append(f"a locked J awaiting its price must show the judgment as "
                             f"waiting, not failed: {by['t_a_b']}")
            lines_p, open_p = render_done(
                [r for r in rows if r["match_id"] == "t_a_b"], ("clean", ""))
            if not open_p or "waiting for the price" not in lines_p[-1]:
                fails.append("waiting items must keep the week not done, and say why")
            store_io.append("odds", {"match_id": "t_a_b", "book": "b", "market": "1x2"},
                            era=CONFIG.era, match_id="t_a_b")
            rows = done_rows(comp, 6, now=now)
            by = {r["match_id"]: dict((c[0], c[1]) for c in r["checks"]) for r in rows}
            if by["t_a_b"].get("judgment") != FAIL:
                fails.append("once priced, a missing judgment is a failure")
            after = datetime(2026, 10, 11, tzinfo=timezone.utc)
            rows = done_rows(comp, 6, now=after)
            by = {r["match_id"]: dict((c[0], c[1]) for c in r["checks"]) for r in rows}
            if by["t_a_b"].get("judgment") != LOSS or by["t_a_b"].get("evidence") != LOSS:
                fails.append(f"after kickoff, a missing evidence bundle or judgment is a "
                             f"recorded LOSS, not something left to fix: {by['t_a_b']}")
        finally:
            store_io.STORE_DIR = old

    # generated scout prompt: built from the schema, not typed
    from football_ai.evidence import schema as _schema
    if set(TEAM_DIMENSIONS) | set(MATCH_DIMENSIONS) != set(_schema.DIMENSIONS) \
            or set(TEAM_DIMENSIONS) & set(MATCH_DIMENSIONS):
        fails.append("the scout split must partition schema.DIMENSIONS exactly: "
                     "a dimension nobody is assigned stays silent forever")
    fx0 = fixtures[0]
    for side, dims in (("home", TEAM_DIMENSIONS), ("away", TEAM_DIMENSIONS),
                       ("match", MATCH_DIMENSIONS)):
        text = scout_prompt(CONFIG, fx0, side, "/tmp/x/bundle.json")
        missing = [d for d in dims if d not in text]
        if missing or "/tmp/x/bundle.json" not in text or "t_a_b" not in text \
                or "價格盲" not in text or "dimension" not in text \
                or any(t not in text for t in _schema.TIERS + _schema.ABSORPTION):
            fails.append(f"scout prompt ({side}) lacks a required element; "
                         f"missing dimensions {missing}")
    try:
        scout_prompt(CONFIG, fx0, "referee", "/tmp/x")
        fails.append("an unknown scout side must be refused")
    except ValueError:
        pass

    # the brief asks for the self-report next to the claims file and passes
    # the betting-site block list to every search
    if str(scout_report_path("/tmp/x/bundle.json")) != "/tmp/x/bundle.scout.json":
        fails.append("the self-report must sit next to the claims file as x.scout.json")
    text = scout_prompt(CONFIG, fx0, "home", "/tmp/x/bundle.json")
    lacking = [t for t in ("/tmp/x/bundle.scout.json", "blocked_domains",
                           *SCOUT_BLOCKED_DOMAINS, *SCOUT_REPORT_FIELDS) if t not in text]
    if lacking:
        fails.append(f"scout prompt lacks {lacking[:4]}")

    # scout self-reports never block J: a missing report is listed, a bad
    # field is kept as None and named, free text with figures is withheld,
    # only the declared fields are kept, and nothing repeats a number
    with tempfile.TemporaryDirectory() as td1:
        d = Path(td1)
        for n in "abcde":
            (d / f"{n}.json").write_text("[]", encoding="utf-8")
        base_rep = {"market_content": "titles of odds pages in search listings, none opened",
                    "market_numbers_seen": True, "page_fetch": "blocked",
                    "search_exhausted": True}
        (d / "a.scout.json").write_text(json.dumps(dict(base_rep, notes="extra")), encoding="utf-8")
        (d / "b.scout.json").write_text(json.dumps(dict(
            base_rep, market_content="a forecasting site had the hosts at 48%")), encoding="utf-8")
        (d / "d.scout.json").write_text(json.dumps(dict(
            base_rep, page_fetch="maybe", search_exhausted="true",
            market_content="headline mentions of odds on several previews")), encoding="utf-8")
        (d / "e.scout.json").mkdir()
        paths = [d / f"{n}.json" for n in "abcde"] + [d / "a.scout.json"]
        sc = load_scout_reports(paths)
        by = {r["file"]: r for r in sc["reports"]}
        if sorted(by) != ["a.json", "b.json", "d.json"] or sc["missing"] != ["c.json", "e.json"] \
                or sc["files"] != 5:
            fails.append(f"reports kept for a, b, d; c and e (a directory) missing; a "
                         f"self-report given as a claims path is ignored: {sc}")
        if set(by.get("a.json", {})) != {"file", *SCOUT_REPORT_FIELDS, "invalid"} \
                or by["a.json"]["invalid"]:
            fails.append(f"a well-formed report keeps only its declared fields: {by.get('a.json')}")
        if by.get("b.json", {}).get("market_content") is not None \
                or not any("withheld" in x for x in by["b.json"]["invalid"]) \
                or "48" in json.dumps(sc):
            fails.append("free text with figures in it must be withheld, never stored or shown")
        dd = by.get("d.json", {})
        if dd.get("page_fetch") is not None or dd.get("search_exhausted") is not None \
                or dd.get("market_numbers_seen") is not True \
                or dd.get("market_content") is not None \
                or set(dd.get("invalid", [])) != {"page_fetch", "search_exhausted",
                                                  "market_content (contained figures; withheld)"}:
            fails.append(f"a bad field is unknown (None) and named, the rest kept: {dd}")
        if [c for c in load_claims(paths)] != []:
            fails.append("load_claims must skip self-report files a glob picks up")
        res = check_evidence(CONFIG, [good], scout=sc)
        shown = "\n".join(render_evidence_check(res))
        if evidence_exit_code(res) == 1 or "48" in shown or "scout reports: 3/5" not in shown \
                or "recorded as unknown" not in shown:
            fails.append(f"self-reports must be shown and must never reject a bundle: {shown!r}")
        if not any("snippets only" in x for x in render_scout_reports(
                {"files": 1, "reports": [by["a.json"]], "missing": []})):
            fails.append("a bundle where every scout was blocked must say J will stay at M")

        # stored next to the claims. Readers take the latest row, so dedup
        # compares with it; a re-store without reports never hides reports
        # on record; a report text with a market number is refused at the
        # store and nothing is written
        old_s = store_io.STORE_DIR
        with tempfile.TemporaryDirectory() as td2:
            store_io.STORE_DIR = Path(td2)
            try:
                a_rep = by["a.json"]
                clean = {"files": 1, "reports": [a_rep], "missing": []}
                late = {"files": 1, "reports": [], "missing": ["a.json"]}
                other = {"files": 1, "reports": [dict(a_rep, search_exhausted=False)],
                         "missing": []}
                got = [ops.store_evidence(CONFIG, "t_a_b", [good], scout_reports=x)["evidence_stored"]
                       for x in (late, late, clean, None, late, other, clean, clean)]
                rows = store_io.read("evidence", era=CONFIG.era)
                if got != [True, False, True, False, False, True, True, False] \
                        or rows[-1]["data"].get("scout_reports") != {"reports": [a_rep],
                                                                      "missing": []}:
                    fails.append(f"store_evidence dedup must follow the latest row and never "
                                 f"hide stored reports: {got}")
                try:
                    ops.store_evidence(CONFIG, "t_x", [good], scout_reports={"reports": [
                        {"file": "x", "market_content": "the prediction market had them at 48%"}]})
                    fails.append("a report text with a market number must be refused at the store")
                except ValueError as e:
                    if "48" in str(e):
                        fails.append("the store refusal must not repeat the number")
                if any(r.get("match_id") == "t_x" for r in store_io.read("evidence", era=CONFIG.era)):
                    fails.append("a refused bundle must leave nothing in the store")
                # whatever the loader keeps, the store accepts: a report never
                # blocks its bundle (a price in words has no digit to catch)
                try:
                    ops.store_evidence(CONFIG, "t_z", [good], scout_reports=sc)
                except ValueError as e:
                    fails.append(f"the store refused reports the loader passed: {e}")
                try:
                    ops.store_evidence(CONFIG, "t_y", [good], scout_reports={"reports": []})
                except KeyError as e:
                    fails.append(f"a partial scout_reports dict must not crash the store: {e}")
            finally:
                store_io.STORE_DIR = old_s

    # evidence sources: a refused tunnel is a policy block; ANY answer from
    # the site itself, a 403 included, means the egress is open
    import urllib.error

    def raising(exc):
        def opener(req, timeout=None):
            raise exc
        return opener
    probes = {
        "tunnel refused": (raising(urllib.error.URLError(OSError(
            "Tunnel connection failed: 403 Forbidden"))), "blocked"),
        "site says 403": (raising(urllib.error.HTTPError(
            "https://x/", 403, "Forbidden", {}, None)), "reachable"),
        "timeout": (raising(urllib.error.URLError(TimeoutError("timed out"))), "unreachable"),
        "answered": (lambda req, timeout=None: object(), "reachable"),
    }
    for name, (opener, want) in probes.items():
        got = _probe_host("example.org", opener=opener)
        if got != want:
            fails.append(f"probe '{name}' must read as {want}, got {got}")
    hosts = [h for hs in EVIDENCE_HOSTS.values() for h in hs]
    stats0 = EVIDENCE_HOSTS["stats"][0]
    cases = [
        ("everything refused", {h: "blocked" for h in hosts}, WARN),
        ("stats refused, news open", {h: ("blocked" if h in EVIDENCE_HOSTS["stats"]
                                          else "reachable") for h in hosts}, WARN),
        ("one stats host open", {h: ("blocked" if h == stats0 else "reachable")
                                 for h in hosts}, OK),
        ("offline", {h: "unreachable" for h in hosts}, UNCHECKED),
        ("all open", {h: "reachable" for h in hosts}, OK),
    ]
    for name, state, want in cases:
        got, detail = _evidence_sources_check(probe=state.get)
        if got != want:
            fails.append(f"evidence sources '{name}' must be {want}, got {got}: {detail}")
    if "still blocked: " + stats0 not in _evidence_sources_check(
            probe={h: ("blocked" if h == stats0 else "reachable") for h in hosts}.get)[1]:
        fails.append("a partly blocked environment must still name what is blocked")

    # generated judgment brief: every number from the ledger / report
    old_store = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outd:
        store_io.STORE_DIR = Path(td)
        try:
            def lk(mid, j=None):
                payload = {"match_id": mid, "kickoff_utc": "2026-10-10T14:00:00+00:00",
                           "teams": {"home": "A", "away": "B"},
                           "probs_90": {"home": .50, "draw": .25, "away": .25}}
                if j:
                    payload["j"] = j
                store_io.append("locks", {"version": 1, "payload": payload},
                                era=CONFIG.era, match_id=mid, version=1)

            def ev(mid, silent):
                store_io.append("evidence", {"match_id": mid, "claims": [{"claim": "c"}],
                                             "coverage": {"covered": ["availability"], "silent": silent}},
                                era=CONFIG.era, match_id=mid)

            def price(mid):
                store_io.append("odds", {"match_id": mid, "book": "b", "market": "1x2"},
                                era=CONFIG.era, match_id=mid)
            jblk = {"probs_90": {"home": .52, "draw": .24, "away": .24},
                    "factors": [{"mechanism_type": "other"}], "basis": ["b"],
                    "runner": {"k_valid": 3}}
            fxs = [{"match_id": m, "gameweek": 7, "home_team": "A", "away_team": "B",
                    "date": "2026-10-10", "kickoff_utc": "2026-10-10T14:00:00+00:00"}
                   for m in ("ok", "owed", "silent", "forecl", "noev", "wait")]
            lk("ok", jblk); price("ok"); ev("ok", [])
            lk("owed"); ev("owed", [])
            lk("silent", jblk); price("silent"); ev("silent", ["referee"])
            lk("forecl"); price("forecl"); ev("forecl", [])
            lk("noev", jblk); price("noev")
            lk("wait", jblk); ev("wait", [])
            fxs.append({"match_id": "flat", "gameweek": 7, "home_team": "A", "away_team": "B",
                        "date": "2026-10-10", "kickoff_utc": "2026-10-10T14:00:00+00:00"})
            lk("flat", {**jblk, "factors": []}); price("flat"); ev("flat", [])

            def fake_build(mid):
                return {"markets": [{"market": "1x2", "book": "b",
                                     "market_fair": {"home": .60, "draw": .22, "away": .18},
                                     "odds": {"home": 1.6, "draw": 4.4, "away": 5.5},
                                     "best_book": {"home": "b", "draw": "b", "away": "c"}}]}
            comp2 = {"config": CONFIG, "upcoming_fixtures": fxs}
            res = judgment_brief(comp2, 7, now=now, build_fn=fake_build, out_dir=outd)
            got = {m["mid"]: m for m in res["args"]["matches"]}
            why = {x["match_id"]: x["reason"] for x in res["skipped"]}
            if res["args"].get("today") != "2026-10-06":
                fails.append("the brief must carry today's date: the workflow used to hard-code one")
            if res["args"].get("depth") != {"escalate_pp": CONFIG.gate.watch_pp}:
                fails.append("the brief must carry the review-depth threshold from the gate, "
                             f"not a typed number: {res['args'].get('depth')}")
            if set(got) != {"ok", "forecl", "flat"}:
                fails.append(f"only fixtures with a resolved J and complete evidence "
                             f"may be briefed, got {sorted(got)}")
            if "price" not in why.get("wait", ""):
                fails.append(f"a J-locked fixture still waiting for its price must be "
                             f"skipped, not briefed with an empty market: {why}")
            if "J is still owed" not in why.get("owed", "") \
                    or "silent" not in why.get("silent", "") \
                    or "no stored evidence" not in why.get("noev", ""):
                fails.append(f"every fixture left out must say why: {why}")
            m = got.get("ok", {})
            if m.get("market", {}).get("devigged") != {"home": .60, "draw": .22, "away": .18} \
                    or m["market"]["gap_pp"] != 10.0 \
                    or [p["book"] for p in m["market"]["best_prices"]] != ["b", "b", "c"] \
                    or [p["decimal_odds"] for p in m["market"]["best_prices"]] != [1.6, 4.4, 5.5]:
                fails.append(f"the market block must be the report's numbers, "
                             f"never typed: {m.get('market')}")
            if m.get("j_status") != "locked" or m.get("j_probs") != jblk["probs_90"] \
                    or got["forecl"]["j_status"] != "foreclosed" or got["forecl"]["j_probs"] is not None:
                fails.append("j_status/j_probs must follow the ledger")
            if m.get("live_divergence") is not False \
                    or got["flat"].get("live_divergence") is not True \
                    or got["forecl"].get("live_divergence") is not True \
                    or "zero factors" not in got["flat"].get("divergence_reason", ""):
                fails.append("from-zero forensics must run exactly where no estimate independent "
                             "of M exists (J missing, or locked with zero factors): "
                             f"{[(k, v.get('live_divergence')) for k, v in got.items()]}")
            if not Path(m["bundle_file"]).exists() or \
                    json.loads(Path(m["bundle_file"]).read_text(encoding="utf-8")) != [{"claim": "c"}]:
                fails.append("bundle_file must hold the stored claims, outside the repo")
        finally:
            store_io.STORE_DIR = old_store

    # preflight: a failing component makes it NO-GO; unchecked does not
    saved = (_sync_check, _claude_probe, _odds_key_check, _actions_health,
             _evidence_sources_check)
    try:
        globals().update(_sync_check=lambda: (OK, ""), _claude_probe=lambda: (OK, ""),
                         _odds_key_check=lambda: (UNCHECKED, "x"), _actions_health=lambda: (UNCHECKED, "x"),
                         _evidence_sources_check=lambda: (OK, ""))
        items = preflight(comp, probe=True, now=now)
        lines, failed = render_preflight(items)
        # t_c_d has a date inside 14 days and no kickoff
        if not failed or "kickoff" not in " ".join(n for n, s, _ in items if s == FAIL):
            fails.append(f"an unconfirmed kickoff inside the horizon must be a NO-GO: {lines}")
        globals().update(_claude_probe=lambda: (FAIL, "no claude"))
        _, failed2 = render_preflight(preflight(
            {"config": CONFIG, "upcoming_fixtures": [fixtures[0]]}, probe=True, now=now))
        if not failed2:
            fails.append("a J subprocess that cannot run must be a NO-GO")
        globals().update(_claude_probe=lambda: (OK, ""))
        _, failed3 = render_preflight(preflight(
            {"config": CONFIG, "upcoming_fixtures": [fixtures[0]]}, probe=True, now=now))
        if failed3:
            fails.append("unchecked items alone must not make preflight a NO-GO")
        globals().update(_evidence_sources_check=lambda: (WARN, "blocked"))
        items4 = preflight({"config": CONFIG, "upcoming_fixtures": [fixtures[0]]},
                           probe=True, now=now)
        lines4, failed4 = render_preflight(items4)
        if failed4 or "WARNING: evidence sources" not in lines4[-1]:
            fails.append("scouts that cannot open pages must be a loud warning on the "
                         "RESULT line, not a NO-GO (locking M is still right)")
    finally:
        globals().update(_sync_check=saved[0], _claude_probe=saved[1],
                         _odds_key_check=saved[2], _actions_health=saved[3],
                         _evidence_sources_check=saved[4])
    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
