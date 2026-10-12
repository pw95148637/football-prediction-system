"""Track-record path for the two-question gate (DECISION_PROTOCOL section 5,
pre-registered 2026-10-10). The criteria were fixed before anyone had
scored the judgment layer against the close -- but not before results
existed: about seven earlier-round judgments had already settled.

The judgment layer's own record against the closing price, measured exactly
the way the charter fixed it in advance:

  sample  the latest judgment row per fixture written BEFORE kickoff, with a
          stored closing 1x2 price (`closing`) and a 90-minute result, this
          era only (no other window counts)
  metric  d = RPS(close, Shin-devigged) - RPS(judgment); d > 0 means the
          judgment was closer to what happened
  CI      fixture-level bootstrap, N_BOOT resamples, 95% percentile interval
  active  n >= MIN_N and the CI lower bound > 0, recomputed on every call,
          so it lapses again as soon as the lower bound falls to 0

While inactive, `judgment.record` refuses `mechanism_grade="track_record"`.
The criteria are module constants so that changing one shows up as a diff
against the charter, never as a quiet edit made after seeing results.

Secondary readings ride along and decide nothing on their own: how often
the close moved toward the judgment from the price it was recorded against,
and d on the subset where the judgment sat >= BAND_PP from that price.
"""
import random
from datetime import datetime

MIN_N = 40
N_BOOT = 10_000
CI_LEVEL = 0.95
BAND_PP = 10.0
SEED = 20261010
OUTCOMES = ("home", "draw", "away")


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _closing_fair(snaps):
    """Shin-devigged closing 1x2, Pinnacle first; None without a stored
    closing snapshot (a last pre-kickoff price is not a close here)."""
    from football_ai.market.devig import devig_shin
    closers = [s for s in snaps if s.get("market") == "1x2" and s.get("closing")]
    if not closers:
        return None
    pin = [s for s in closers if s.get("book") == "pinnacle"]
    snap = max(pin or closers, key=lambda s: s.get("fetched_at") or "")
    probs = devig_shin(snap["odds"])
    return dict(zip(snap["outcomes"], probs)), snap.get("book")


def _bootstrap_ci(values, n_boot, seed):
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(n_boot))
    lo_i = int(n_boot * (1 - CI_LEVEL) / 2)
    hi_i = int(n_boot * (1 + CI_LEVEL) / 2) - 1
    return means[lo_i], means[hi_i]


def scorecard(era, n_boot=N_BOOT, seed=SEED):
    from football_ai.research.metrics import rps
    from football_ai.store import io as store_io

    results = {r["data"]["match_id"]: r["data"]["score90"]
               for r in store_io.read("results", era=era)
               if r.get("data", {}).get("score90")}
    kickoff = {}
    for r in store_io.read("locks", era=era):
        k = (r["data"].get("payload") or {}).get("kickoff_utc")
        if k and r.get("match_id") not in kickoff:
            kickoff[r["match_id"]] = k
    judgments = {}
    for r in store_io.read("judgments", era=era):
        judgments.setdefault(r.get("match_id"), []).append(r)
    odds = {}
    for r in store_io.read("odds", era=era):
        odds.setdefault(r.get("match_id"), []).append(r["data"])

    rows, excluded = [], {}
    for mid, score in sorted(results.items()):
        ko = kickoff.get(mid)
        pre = [j for j in judgments.get(mid, [])
               if ko and _ts(j["ts"]) < _ts(ko)
               and (j["data"].get("judgment_probs_90") or {})]
        if not pre:
            excluded[mid] = "no judgment written before kickoff"
            continue
        jd = pre[-1]["data"]
        close = _closing_fair(odds.get(mid, []))
        if close is None:
            excluded[mid] = "no stored closing price"
            continue
        fair, book = close
        hs, as_ = score
        oi = 0 if hs > as_ else (1 if hs == as_ else 2)
        pj = [jd["judgment_probs_90"][k] for k in OUTCOMES]
        pc = [fair[k] for k in OUTCOMES]
        row = {"match_id": mid, "d": rps(pc, oi) - rps(pj, oi), "close_book": book,
               "toward": None, "band": False}
        entry = jd.get("market_devigged")
        if entry:
            dirn = jd.get("direction")
            lean = jd["judgment_probs_90"].get(dirn)
            if dirn in fair and lean is not None:
                moved = fair[dirn] - entry[dirn]
                side = lean - entry[dirn]
                if moved and side:
                    row["toward"] = (moved > 0) == (side > 0)
            row["band"] = 100 * max(abs(jd["judgment_probs_90"][k] - entry[k])
                                    for k in OUTCOMES) >= BAND_PP
        rows.append(row)

    ds = [r["d"] for r in rows]
    n = len(ds)
    ci = _bootstrap_ci(ds, n_boot, seed) if n >= 2 else (None, None)
    band = [r["d"] for r in rows if r["band"]]
    toward = [r["toward"] for r in rows if r["toward"] is not None]
    return {
        "n": n, "mean_d": (sum(ds) / n) if n else None, "ci": ci,
        "active": bool(n >= MIN_N and ci[0] is not None and ci[0] > 0),
        "min_n": MIN_N,
        "band_n": len(band), "band_mean_d": (sum(band) / len(band)) if band else None,
        "toward_n": len(toward),
        "toward_frac": (sum(toward) / len(toward)) if toward else None,
        "excluded": excluded, "rows": rows,
    }


def render(sc):
    if not sc["n"]:
        return (f"研判實績(實績路徑):尚無可計分場次(需開球前研判、收盤價與賽果);"
                f"生效須 n ≥ {sc['min_n']} 且信賴區間下限 > 0。未生效。")
    lo, hi = sc["ci"]
    ci = f"[{lo:+.4f}, {hi:+.4f}]" if lo is not None else "(n<2,無區間)"
    tw = (f";收盤價向研判移動 {sc['toward_frac']:.0%}(n={sc['toward_n']})"
          if sc["toward_n"] else "")
    return (f"研判實績(實績路徑):n={sc['n']},d=RPS(收盤)−RPS(研判) 平均 "
            f"{sc['mean_d']:+.4f},95% CI {ci}{tw}。"
            f"{'已生效' if sc['active'] else '未生效'}"
            f"(須 n ≥ {sc['min_n']} 且下限 > 0)。")


def _selfcheck():
    import tempfile
    from pathlib import Path
    from football_ai.store import io as store_io
    fails = []
    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        era = "t-track"
        try:
            def fixture(mid, judged_at="2026-10-01T10:00:00+00:00", close=True,
                        jp=(0.60, 0.25, 0.15), score=(1, 0), entry=(0.50, 0.28, 0.22)):
                store_io.append("locks", {"version": 1, "payload": {
                    "match_id": mid, "kickoff_utc": "2026-10-01T14:00:00+00:00",
                    "probs_90": {"home": .5, "draw": .25, "away": .25}}},
                    era=era, match_id=mid, version=1)
                if judged_at:
                    store_io.append("judgments", {
                        "match_id": mid, "direction": "home",
                        "judgment_probs_90": dict(zip(OUTCOMES, jp)),
                        "market_devigged": dict(zip(OUTCOMES, entry))},
                        era=era, match_id=mid, ts=judged_at)
                if close:
                    store_io.append("odds", {"match_id": mid, "book": "pinnacle",
                                             "market": "1x2", "line": None,
                                             "outcomes": list(OUTCOMES),
                                             "odds": [1.85, 3.6, 4.6], "closing": True,
                                             "fetched_at": "2026-10-01T13:05:00+00:00"},
                                    era=era, match_id=mid)
                store_io.append("results", {"match_id": mid, "score90": list(score)},
                                era=era, match_id=mid)

            fixture("late", judged_at="2026-10-01T15:00:00+00:00")
            fixture("noclose", close=False)
            fixture("unjudged", judged_at=None)
            for i in range(MIN_N - 1):
                fixture(f"m{i:02d}")
            sc = scorecard(era, n_boot=500)
            if set(sc["excluded"]) != {"late", "noclose", "unjudged"}:
                fails.append(f"post-kickoff, close-less and unjudged fixtures must be "
                             f"excluded with a reason: {sc['excluded']}")
            if sc["n"] != MIN_N - 1 or sc["active"]:
                fails.append(f"n={MIN_N - 1} must not activate however good: {sc['n']}, {sc['active']}")
            # a home win the judgment called more confidently than the close: d > 0
            if not (sc["mean_d"] and sc["mean_d"] > 0):
                fails.append(f"a sharper judgment must score d > 0: {sc['mean_d']}")
            from football_ai.decision import judgment
            probe = {"match_id": "probe", "mechanism_grade": "track_record",
                     "model": "selfcheck"}
            try:
                judgment.record(probe, era, validate=False, check_coverage=False)
                fails.append("record() must refuse mechanism_grade 'track_record' while "
                             "the track record is inactive")
            except ValueError:
                pass
            if sc["toward_frac"] != 1.0:
                fails.append(f"the close (home ~0.52) sits on the judgment's side of the "
                             f"0.50 entry: toward must count it -- got {sc['toward_frac']}")
            fixture(f"m{MIN_N - 1:02d}")
            sc = scorecard(era, n_boot=500)
            if not sc["active"]:
                fails.append(f"n={MIN_N} with every d > 0 must activate: {sc['ci']}")
            else:
                judgment.record(probe, era, validate=False, check_coverage=False)
                row = store_io.read("judgments", era=era)[-1]["data"]
                if (row.get("track_record") or {}).get("n") != MIN_N:
                    fails.append(f"an accepted 'track_record' row must carry the measured "
                                 f"record it relied on: {row.get('track_record')}")
            for i in range(MIN_N):
                fixture(f"x{i:02d}", jp=(0.30, 0.30, 0.40))
            sc = scorecard(era, n_boot=500)
            if sc["active"]:
                fails.append(f"a record whose lower bound falls to <= 0 must lapse: {sc['ci']}")
        finally:
            store_io.STORE_DIR = old
    return fails
