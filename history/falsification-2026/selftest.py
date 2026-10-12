"""Self-tests for the wc2026 harness: devig, model, metrics, lock mechanism.

Run: python wc2026/selftest.py  (exit 0 = all pass)
Deliberately standalone from the main tool's pytest suite/grader.
"""
import json
import math
import tempfile
from pathlib import Path

from devig import devig_additive, devig_shin, devig_both
from model import (ah_probs, brier, elo_expected, outcome_probs,
                   qualify_prob, rps, solve_lambdas, totals_probs)
from lock import amend_lock, create_lock, verify_lock

FAILS = []


def check(name, cond):
    print(f"  {'ok' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


print("devig:")
for odds in ([2.0, 3.4, 3.8], [1.35, 5.2, 9.5], [1.91, 1.91]):
    s, a = devig_shin(odds), devig_additive(odds)
    check(f"shin sums to 1 {odds}", approx(sum(s), 1.0, 1e-9))
    check(f"additive sums to 1 {odds}", approx(sum(a), 1.0, 1e-9))
    check(f"shin in (0,1) {odds}", all(0 < p < 1 for p in s))
check("symmetric 2-way -> 50/50 (shin)",
      all(approx(p, 0.5, 1e-9) for p in devig_shin([1.91, 1.91])))
check("symmetric 2-way -> 50/50 (additive)",
      all(approx(p, 0.5, 1e-9) for p in devig_additive([1.91, 1.91])))
check("no-margin odds returned proportionally",
      all(approx(p, 1 / 3, 1e-9) for p in devig_shin([3.0, 3.0, 3.0])))
check("shin shades longshots vs proportional",
      devig_shin([1.35, 5.2, 9.5])[2] < (1 / 9.5) / sum(1 / o for o in [1.35, 5.2, 9.5]))
check("devig_both reports diff", "max_diff_pp" in devig_both([2.0, 3.4, 3.8]))

print("model:")
lh, la = solve_lambdas(0.0, total_goals=2.4)
check("elo_diff=0 -> equal lambdas", approx(lh, la, 1e-6) and approx(lh + la, 2.4, 1e-9))
w, d, l = outcome_probs(lh, la)
check("elo_diff=0 -> expected score 0.5", approx(w + 0.5 * d, 0.5, 1e-6))
check("elo_diff=0 -> symmetric outcome", approx(w, l, 1e-6))

lh, la = solve_lambdas(200.0, total_goals=2.4)
w, d, l = outcome_probs(lh, la)
check("elo_diff=200 -> expected score matches Elo",
      approx(w + 0.5 * d, elo_expected(200.0), 1e-6))
check("elo_diff=200 -> favorite has higher lambda", lh > la)

over, push, under = totals_probs(1.3, 1.1, 2.5)
check("totals sums to 1", approx(over + push + under, 1.0, 1e-6))
check("half-goal total has no push", approx(push, 0.0, 1e-12))
oq, pq, uq = totals_probs(1.3, 1.1, 2.25)
o0, p0, u0 = totals_probs(1.3, 1.1, 2.0)
check("quarter total = mean of adjacent lines",
      approx(oq, (o0 + over) / 2, 1e-12) and approx(uq, (u0 + under) / 2, 1e-12))

w0, p0, l0 = ah_probs(1.3, 1.1, 0.0)
w5, p5, l5 = ah_probs(1.3, 1.1, -0.5)
wq, pq, lq = ah_probs(1.3, 1.1, -0.25)
check("quarter line = mean of adjacent lines",
      approx(wq, (w0 + w5) / 2, 1e-12) and approx(pq, (p0 + p5) / 2, 1e-12)
      and approx(lq, (l0 + l5) / 2, 1e-12))
check("ah(0) push equals draw prob", approx(p0, outcome_probs(1.3, 1.1)[1], 1e-9))

check("qualify symmetric -> 0.5", approx(qualify_prob(1.2, 1.2, 0.5), 0.5, 1e-6))
check("qualify favors higher lambda", qualify_prob(1.5, 0.9) > 0.5)
check("qualify pk lever works",
      qualify_prob(1.2, 1.2, 0.7) > qualify_prob(1.2, 1.2, 0.5))

print("metrics:")
check("rps known value", approx(rps([0.8, 0.1, 0.1], 0), 0.025, 1e-12))
check("rps perfect forecast = 0", approx(rps([1.0, 0.0, 0.0], 0), 0.0, 1e-12))
check("brier perfect forecast = 0", approx(brier([1.0, 0.0, 0.0], 0), 0.0, 1e-12))
check("rps rewards closer forecast",
      rps([0.6, 0.25, 0.15], 0) < rps([0.4, 0.3, 0.3], 0))
check("rps ordered: mass on draw beats mass on away when home wins",
      rps([0.3, 0.5, 0.2], 0) < rps([0.3, 0.2, 0.5], 0))

print("calibrated M v2:")
try:
    from model import (load_calibration, predict_1x2, predict_lambdas,
                       qualify_prob_v2)
    load_calibration()
    w, d, l = predict_1x2(0.0)
    check("neutral d=0 symmetric", approx(w, l, 1e-9) and approx(w + d + l, 1.0, 1e-9))
    check("draw mass sane at d=0", 0.20 < d < 0.40)
    check("win monotone in d",
          predict_1x2(200)[0] > predict_1x2(100)[0] > predict_1x2(0)[0])
    check("probs sum to 1 across d",
          all(approx(sum(predict_1x2(dd)), 1.0, 1e-9)
              for dd in (-400, -100, 50, 300, 600)))
    lh, la = predict_lambdas(100)
    check("lambda favors stronger side", lh > la > 0)
    check("qualify_v2 symmetric = 0.5", approx(qualify_prob_v2(0.0), 0.5, 1e-6))
    check("qualify_v2 monotone", qualify_prob_v2(300) > qualify_prob_v2(100) > 0.5)
except FileNotFoundError:
    print("  skip (calibration.json not present — run calibrate.py)")

print("score plumbing:")
from score import closing_snapshot, settle_bet

SNAPS = [
    {"book": "polymarket", "market": "1x2", "line": None,
     "odds": [2.0, 3.0, 4.0], "outcomes": ["home", "draw", "away"],
     "fetched_at": "2026-01-01T00:00:00+00:00", "closing": False},
    {"book": "pinnacle", "market": "1x2", "line": None,
     "odds": [2.1, 3.1, 4.1], "outcomes": ["home", "draw", "away"],
     "fetched_at": "2026-01-01T01:00:00+00:00", "closing": False},
    {"book": "pinnacle", "market": "1x2", "line": None,
     "odds": [2.2, 3.2, 4.2], "outcomes": ["home", "draw", "away"],
     "fetched_at": "2026-01-01T02:00:00+00:00", "closing": True},
]
KO = "2026-01-01T03:00:00+00:00"
check("closing flag preferred over later fetch",
      closing_snapshot(SNAPS, "1x2")["odds"][0] == 2.2)
check("pre-kickoff fallback picks last pinnacle fetch",
      closing_snapshot(SNAPS[:2], "1x2", kickoff_utc=KO)["odds"][0] == 2.1)
check("book=None falls back to any bookmaker",
      closing_snapshot(SNAPS[:1], "1x2", book=None,
                       kickoff_utc=KO)["odds"][0] == 2.0)
check("pinnacle-only returns None when absent",
      closing_snapshot(SNAPS[:1], "1x2", kickoff_utc=KO) is None)

check("1x2 win pnl", approx(settle_bet(
    {"market": "1x2", "selection": "home", "odds_taken": 3.4,
     "stake_u": 1.0}, {"score90": [2, 0]}), 2.4, 1e-9))
check("1x2 loss pnl", approx(settle_bet(
    {"market": "1x2", "selection": "draw", "odds_taken": 3.0,
     "stake_u": 1.0}, {"score90": [2, 0]}), -1.0, 1e-9))
check("ah -0.25 on draw = half loss", approx(settle_bet(
    {"market": "ah", "selection": "home", "line": -0.25,
     "odds_taken": 2.0, "stake_u": 1.0}, {"score90": [1, 1]}), -0.5, 1e-9))
check("ah -0.75 on one-goal win = half win", approx(settle_bet(
    {"market": "ah", "selection": "home", "line": -0.75,
     "odds_taken": 2.0, "stake_u": 1.0}, {"score90": [1, 0]}), 0.5, 1e-9))
check("over 2.25 with 2 goals = half loss", approx(settle_bet(
    {"market": "totals", "selection": "over", "line": 2.25,
     "odds_taken": 2.0, "stake_u": 1.0}, {"score90": [1, 1]}), -0.5, 1e-9))
check("qualify win pnl", approx(settle_bet(
    {"market": "qualify", "selection": "away", "odds_taken": 2.05,
     "stake_u": 1.0}, {"score90": [1, 4], "advanced": "away"}), 1.05, 1e-9))
check("qualify loss pnl", approx(settle_bet(
    {"market": "qualify", "selection": "home", "odds_taken": 2.05,
     "stake_u": 1.0}, {"score90": [1, 4], "advanced": "away"}), -1.0, 1e-9))
check("qualify without advanced field -> None (unsettled)", settle_bet(
    {"market": "qualify", "selection": "away", "odds_taken": 2.05,
     "stake_u": 1.0}, {"score90": [1, 1]}) is None)
check("unknown market -> None (unsettled)", settle_bet(
    {"market": "correct_score", "selection": "2-0", "odds_taken": 6.5,
     "stake_u": 1.0}, {"score90": [2, 0]}) is None)

print("lock:")
payload = {
    "match_id": "m00_test", "teams": {"home": "A", "away": "B"},
    "kickoff_utc": "2099-07-04T17:00:00+00:00", "mode": "A",
    "probs_90": {"home": 0.45, "draw": 0.27, "away": 0.28},
    "qualify_home": 0.56, "lambdas": {"home": 1.3, "away": 1.1},
    "inputs": {"elo_home": 1900, "elo_away": 1850, "total_goals": 2.4},
    "rationale": ["test"], "invalidation": ["test"],
    "sources": [{"item": "elo", "source": "test", "as_of": "2026-07-02"}],
}
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    path = create_lock(payload, locks_dir=d)
    ok, doc = verify_lock(path)
    check("create+verify roundtrip", ok and doc["version"] == 1)
    try:
        create_lock(payload, locks_dir=d)
        check("overwrite refused", False)
    except FileExistsError:
        check("overwrite refused", True)
    p2 = dict(payload, probs_90={"home": 0.5, "draw": 0.25, "away": 0.25})
    ap = amend_lock("m00_test", p2, "lineup announced: key striker out", locks_dir=d)
    ok2, doc2 = verify_lock(ap)
    check("amend writes v2, verifies", ok2 and doc2["version"] == 2)
    check("amend keeps original hash ref",
          doc2["original_sha256"] == doc["sha256"])
    check("original untouched",
          verify_lock(d / "m00_test.json")[0])
    try:
        amend_lock("m00_test", p2, "   ", locks_dir=d)
        check("amend requires reason", False)
    except ValueError:
        check("amend requires reason", True)
    bad = dict(payload, probs_90={"home": 0.5, "draw": 0.3, "away": 0.3})
    try:
        create_lock(dict(bad, match_id="m01_bad"), locks_dir=d)
        check("probs must sum to 1", False)
    except ValueError:
        check("probs must sum to 1", True)
    pj = dict(payload, match_id="m02_j",
              j={"probs_90": {"home": 0.4, "draw": 0.3, "away": 0.3},
                 "qualify_home": 0.5, "basis": ["Opta forecast", "FBref xG"]})
    create_lock(pj, locks_dir=d)
    check("lock with instrument J accepted", verify_lock(d / "m02_j.json")[0])
    try:
        create_lock(dict(pj, match_id="m03_jbad",
                         j={"probs_90": {"home": 0.5, "draw": 0.3,
                                         "away": 0.3},
                            "qualify_home": 0.5, "basis": ["x"]}),
                    locks_dir=d)
        check("J probs must sum to 1", False)
    except ValueError:
        check("J probs must sum to 1", True)
    try:
        create_lock(dict(pj, match_id="m04_jnb",
                         j={"probs_90": {"home": 0.4, "draw": 0.3,
                                         "away": 0.3},
                            "qualify_home": 0.5, "basis": []}),
                    locks_dir=d)
        check("J requires basis", False)
    except ValueError:
        check("J requires basis", True)

print("score composition (P0 fix, Architecture Review v3 section 2.1 / "
      "Migration Plan Phase 0(a) — pilot matches must never enter the "
      "pre-registered 16-match sample or its section-4 verdict trigger):")
import score as _score


def _pilot_test_payload(match_id, pilot):
    return {
        "match_id": match_id, "pilot": pilot,
        "teams": {"home": "A", "away": "B"},
        "kickoff_utc": "2099-07-04T17:00:00+00:00", "mode": "A",
        "probs_90": {"home": 0.5, "draw": 0.25, "away": 0.25},
        "qualify_home": 0.6, "lambdas": {"home": 1.3, "away": 1.1},
        "inputs": {"elo_home": 1900, "elo_away": 1850, "total_goals": 2.4},
        "rationale": ["test"], "invalidation": ["test"],
        "sources": [{"item": "elo", "source": "test", "as_of": "2026-07-02"}],
    }


with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    locks_td, odds_td = tdp / "locks", tdp / "odds"
    locks_td.mkdir()
    odds_td.mkdir()
    create_lock(_pilot_test_payload("m_main_test", False), locks_dir=locks_td)
    create_lock(_pilot_test_payload("r32_pilot_test", True), locks_dir=locks_td)
    for mid in ("m_main_test", "r32_pilot_test"):
        (odds_td / f"{mid}.jsonl").write_text(json.dumps({
            "book": "pinnacle", "market": "1x2", "line": None,
            "odds": [2.0, 3.4, 3.8], "outcomes": ["home", "draw", "away"],
            "fetched_at": "2026-07-05T00:00:00+00:00", "closing": True,
        }) + "\n", encoding="utf-8")
    fake_results = {"m_main_test": {"score90": [1, 0]},
                    "r32_pilot_test": {"score90": [1, 0]}}

    # load_odds's default `odds_dir=ODDS_DIR` binds at def-time, so patching
    # score.ODDS_DIR alone would not reach it — patch the function itself.
    old_load_odds = _score.load_odds
    _score.load_odds = lambda mid, odds_dir=odds_td: old_load_odds(mid, odds_dir=odds_td)
    try:
        rows = _score.score_b1(_score.load_locks(locks_td), fake_results)
    finally:
        _score.load_odds = old_load_odds

    by_id = {r["match_id"]: r for r in rows}
    check("main-sample lock flagged pilot=False",
          by_id["m_main_test"]["pilot"] is False)
    check("pilot lock flagged pilot=True",
          by_id["r32_pilot_test"]["pilot"] is True)
    scored_all = [r for r in rows if r.get("status") == "scored"]
    main_only = [r for r in scored_all if not r.get("pilot")]
    check("both synthetic matches scored", len(scored_all) == 2)
    check("pilot excluded from main-sample composition",
          len(main_only) == 1 and main_only[0]["match_id"] == "m_main_test")

print("leak validator (firewall.py, Architecture Review v3 section 4.4 — "
      "the m97 class of failure made mechanical, not prompt-only):")
from firewall import validate_bundle_text

ok, leaks = validate_bundle_text(
    "Kalshi has France trading around 62% to advance, per the prediction "
    "market.")
check("m97-shaped leak (bookmaker word + adjacent %) is caught", not ok and len(leaks) >= 1)
ok2, leaks2 = validate_bundle_text(
    "Opta's supercomputer gives Spain 70.4% pregame; Spain's xG this "
    "tournament is 2.84 per game, Elo 2159 vs Portugal 2013.")
check("analytical numbers (Opta/xG/Elo) pass clean", ok2 and leaks2 == [])
ok3, leaks3 = validate_bundle_text(
    "USMNT opened at -145 on the moneyline before lineups dropped.")
check("american-odds-shaped number is caught even without a book name nearby",
      not ok3 and len(leaks3) >= 1)
ok4, leaks4 = validate_bundle_text(
    "A market participant mentioned pricing but gave no numbers — "
    "url-only exposure, no digits.")
check("book-word with no adjacent number passes (URL/slug-only exposure "
      "is a disclosure matter, not a hard block)", ok4 and leaks4 == [])
# false-positive regression set (2026-07-08 hardening): legitimate evidence
# text must NOT be bounced back — an over-eager gate would train sessions to
# bypass it, which is worse than no gate.
ok5, _ = validate_bundle_text(
    "Flu-like illness spread through the camp; 3 players affected, James "
    "withdrawn at HT.")
check("illness-'spread' team news with counts passes (bare 'spread' is "
      "not a market word)", ok5)
ok6, _ = validate_bundle_text(
    "odds_exposure: URL-only, draftkings.com/soccer slug seen in search "
    "results, accessed 2026-07-08, no numbers read.")
check("exposure declaration (book name + access DATE, no price) passes",
      ok6)
ok7, _ = validate_bundle_text("Elo diff +404 at neutral venue; d_eff -146.")
check("Elo-diff notation (+404/-146) passes (rating context, not moneyline)",
      ok7)
ok8, _ = validate_bundle_text(
    "Celta Vigo loanee played vigorously for 90 minutes, 2 chances created.")
check("'vig' inside Vigo/vigorously does not trip the market-word list", ok8)
ok9, _ = validate_bundle_text(
    "USMNT opened at -145 before lineups dropped.")
check("american-odds shape still caught when context is market ('opened "
      "at'), not rating", not ok9)
ok10, _ = validate_bundle_text(
    "we cross-checked bet365 and sport888 preview pages but copied no "
    "prices; exposure was URL-level only.")
check("digits inside a market keyword itself (bet365/sport888) are brand "
      "chars, not a leak (found live 2026-07-09)", ok10)
ok11, _ = validate_bundle_text("bet365 has Spain at 1.64 for the win.")
check("a real price NEXT TO a digit-bearing keyword is still caught",
      not ok11)
ok12, _ = validate_bundle_text(
    "## 11. Odds exposure (URLs where bookmaker/market content appeared "
    "on-page; NOT extracted, no numbers below)")
check("numbered section heading ('## 11. Odds exposure ... bookmaker...') "
      "passes — the heading's own ordinal is not a price (found live "
      "2026-07-16, m103_fra_eng_prelock.md)", ok12)

print("kickoff deadline guard (lock.py, 2026-07-08 — the m92 lesson made "
      "mechanical: post-kickoff locks/amends are never honest):")
past = dict(payload, match_id="m00_past",
            kickoff_utc="2020-01-01T00:00:00+00:00")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    try:
        create_lock(past, locks_dir=d)
        check("post-kickoff create refused", False)
    except ValueError:
        check("post-kickoff create refused", True)
    create_lock(dict(payload, match_id="m00_future"), locks_dir=d)
    check("pre-kickoff create still works",
          (d / "m00_future.json").exists())
    try:
        amend_lock("m00_future",
                   dict(payload, match_id="m00_future",
                        kickoff_utc="2020-01-01T00:00:00+00:00"),
                   "test", locks_dir=d)
        check("post-kickoff amend refused", False)
    except ValueError:
        check("post-kickoff amend refused", True)

print("J-independence audit (score.py contaminated_j — auto-M-lock flow: "
      "the FIRST version carrying j must precede first odds contact):")
_J = {"probs_90": {"home": 0.4, "draw": 0.3, "away": 0.3},
      "qualify_home": 0.5, "basis": ["test basis"]}


def _write_lock_doc(locks_dir, match_id, locked_at, version, with_j,
                    suffix=""):
    # hand-written docs: score.py's load_locks reads JSON directly and does
    # not re-verify hashes, so scoring-path tests may construct timestamps
    # that create_lock (which stamps real now()) cannot produce.
    p = dict(_pilot_test_payload(match_id, False))
    if with_j:
        p["j"] = _J
    doc = {"locked_at": locked_at, "version": version, "payload": p,
           "sha256": "test"}
    name = f"{match_id}{suffix}.json"
    (locks_dir / name).write_text(json.dumps(doc), encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    locks_td, odds_td = tdp / "locks", tdp / "odds"
    locks_td.mkdir()
    odds_td.mkdir()
    # m_latej: v1 M-only at T0, odds first fetched T1, j added via v2 at T2
    _write_lock_doc(locks_td, "m_latej", "2026-01-01T00:00:00+00:00", 1, False)
    _write_lock_doc(locks_td, "m_latej", "2026-01-03T00:00:00+00:00", 2, True,
                    suffix=".v2")
    # m_cleanj: v1 M-only at T0, j added via v2 BEFORE first odds fetch
    _write_lock_doc(locks_td, "m_cleanj", "2026-01-01T00:00:00+00:00", 1, False)
    _write_lock_doc(locks_td, "m_cleanj", "2026-01-01T12:00:00+00:00", 2, True,
                    suffix=".v2")
    for mid in ("m_latej", "m_cleanj"):
        (odds_td / f"{mid}.jsonl").write_text(json.dumps({
            "book": "pinnacle", "market": "1x2", "line": None,
            "odds": [2.0, 3.4, 3.8], "outcomes": ["home", "draw", "away"],
            "fetched_at": "2026-01-02T00:00:00+00:00", "closing": True,
        }) + "\n", encoding="utf-8")
    fake_results = {"m_latej": {"score90": [1, 0]},
                    "m_cleanj": {"score90": [1, 0]}}
    old_load_odds = _score.load_odds
    _score.load_odds = lambda mid, odds_dir=odds_td: old_load_odds(
        mid, odds_dir=odds_td)
    try:
        rows = {r["match_id"]: r
                for r in _score.score_b1(_score.load_locks(locks_td),
                                         fake_results)}
    finally:
        _score.load_odds = old_load_odds
    check("post-market j flagged contaminated_j",
          rows["m_latej"]["contaminated_j"] is True)
    check("post-market j dropped from J scoring",
          "j_rps" not in rows["m_latej"])
    check("v1 M-lock itself stays uncontaminated (locked before fetch)",
          rows["m_latej"]["contaminated"] is False)
    check("pre-market j via amend stays clean and scored",
          rows["m_cleanj"]["contaminated_j"] is False
          and "j_rps" in rows["m_cleanj"])
    check("j-only amend (M unchanged) does not print anchoring drift",
          "amend_moved_toward_market" not in rows["m_latej"]
          and "amend_moved_toward_market" not in rows["m_cleanj"])

print("section-4 verdict states (score.py — composition + registered-"
      "MISSING completion, thresholds themselves unchanged):")


def _vrow(model_rps, market_rps, with_j=True):
    r = {"model_rps": model_rps, "market_rps": market_rps,
         "model_better": model_rps < market_rps}
    if with_j:
        r["j_rps"] = model_rps
        r["j_better"] = model_rps < market_rps
    return r


good16 = [_vrow(0.10, 0.20) for _ in range(16)]
lines = _score.section4_verdict(good16, [r for r in good16 if "j_rps" in r])
check("n=16 model-ahead -> 'not falsified' fires mechanically",
      any("not falsified" in ln for ln in lines))
bad16 = [_vrow(0.30, 0.20) for _ in range(16)]
lines = _score.section4_verdict(bad16, [r for r in bad16 if "j_rps" in r])
check("n=16 model-behind everywhere -> 'falsified' fires mechanically",
      any("falsified in-domain" in ln for ln in lines))
full15 = [_vrow(0.10, 0.20) for _ in range(15)]
lines = _score.section4_verdict(full15, [r for r in full15 if "j_rps" in r],
                                missing=["m92_mex_eng"])
check("n=15 with registered MISSING -> COMPLETE + deferred adjudication "
      "(never a silent forever-'partial')",
      any("MAIN SAMPLE COMPLETE" in ln for ln in lines)
      and any("final report" in ln for ln in lines))
part7 = [_vrow(0.10, 0.20) for _ in range(7)]
lines = _score.section4_verdict(part7, part7, missing=["m92_mex_eng"])
check("n=7 -> partial sample, no verdict",
      any("partial sample" in ln for ln in lines))
lines = _score.section4_verdict([], [], missing=["m92_mex_eng"])
check("n=0 -> partial sample (empty-safe)",
      any("partial sample" in ln for ln in lines))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES: {FAILS}")
    raise SystemExit(1)
print("ALL SELFTESTS PASS")
