"""Stratified B1 scoring over the store + bit-identical v2 recompute check.

Two jobs (Architecture Review v3 sections 4.5 / 7-Phase-1):

1. RECOMPUTE GATE — score the era-imported locks/odds/results with logic
   ported exactly from wc2026/history/falsification-2026/score.py and verify
   the per-match numbers match
   wc2026/history/falsification-2026/report.json bit-for-bit (1e-12). This
   is the Migration
   Phase 1 acceptance test: the migration must not change a single
   historical number. Run via `python -m football_ai.research.scoring`.

2. STRATIFIED ANALYSIS — the aggregate B1 hides that J=M matches carry no
   information about the J instrument (section 3.2 of the review): the
   hypothesis' real test point is the J-DEVIATED subset. Report main vs
   pilot, and within main: J-deviated vs J=M, with paired per-match RPS
   deltas and a seeded-bootstrap CI (n is tiny; the honest output is a wide
   interval, not a verdict).

Import direction: research/ touches the market only through the frozen
devig copy (de-vigged closes at scoring time) — never consensus/decision.
"""
import json
import random
from pathlib import Path

from football_ai.core.paths import v2_archive_dir
from football_ai.market.devig import devig_shin
from football_ai.research.metrics import brier, rps
from football_ai.store import io as store_io

V2_REPORT = v2_archive_dir() / "report.json"
BOOTSTRAP_SEED = 20260708
BOOTSTRAP_N = 10_000


# --- store access -----------------------------------------------------------

def locks_by_match(era="v2-wc2026"):
    """{match_id: {"v1", "final", "versions", "n_versions"}} — same shape
    score.py's load_locks builds from the lock files."""
    groups = {}
    for rec in store_io.read("locks", era=era):
        groups.setdefault(rec["match_id"], []).append(rec["data"])
    out = {}
    for mid, docs in groups.items():
        docs.sort(key=lambda d: d["version"])
        out[mid] = {"v1": docs[0], "final": docs[-1], "versions": docs,
                    "n_versions": len(docs)}
    return out


def odds_by_match(era="v2-wc2026"):
    out = {}
    for rec in store_io.read("odds", era=era):
        out.setdefault(rec["match_id"], []).append(rec["data"])
    return out


def results_map(era="v2-wc2026"):
    return {rec["data"]["match_id"]: rec["data"]
            for rec in store_io.read("results", era=era)}


# --- scoring (ported exactly from wc2026/score.py) --------------------------

def closing_snapshot(snaps, market="1x2", line=None, book="pinnacle",
                     kickoff_utc=None):
    def line_match(s):
        if line is None and s.get("line") is None:
            return True
        if line is None or s.get("line") is None:
            return False
        return abs(s["line"] - line) < 1e-9

    cands = [s for s in snaps
             if (book is None or s["book"] == book)
             and s["market"] == market and line_match(s)]
    closers = [s for s in cands if s.get("closing")]
    if closers:
        return max(closers, key=lambda s: s["fetched_at"])
    if kickoff_utc:
        pre = [s for s in cands if s["fetched_at"] <= kickoff_utc]
        if pre:
            return max(pre, key=lambda s: s["fetched_at"])
    return None


def outcome_index(score90):
    h, a = score90
    return 0 if h > a else (2 if h < a else 1)


def score_rows(era="v2-wc2026"):
    """Per-match B1 rows, schema-compatible with wc2026/score.py score_b1."""
    locks = locks_by_match(era)
    odds = odds_by_match(era)
    results = results_map(era)
    rows = []
    for mid, entry in sorted(locks.items()):
        payload_v1 = entry["v1"]["payload"]
        kickoff = payload_v1["kickoff_utc"]
        snaps = odds.get(mid, [])
        close = closing_snapshot(snaps, "1x2", kickoff_utc=kickoff)
        if close is None:
            close = closing_snapshot(snaps, "1x2", book=None,
                                     kickoff_utc=kickoff)
        res = results.get(mid)
        row = {"match_id": mid, "n_versions": entry["n_versions"],
               "pilot": bool(payload_v1.get("pilot", False))}
        first_fetch = min((s["fetched_at"] for s in snaps), default=None)
        row["contaminated"] = first_fetch is not None and \
            entry["v1"]["locked_at"] > first_fetch
        j_doc_meta = next(((d["locked_at"], d["payload"]["j"])
                           for d in entry["versions"]
                           if d["payload"].get("j")), None)
        row["contaminated_j"] = j_doc_meta is not None and \
            first_fetch is not None and j_doc_meta[0] > first_fetch
        if close is None or res is None or "score90" not in (res or {}):
            row["status"] = "unscored (missing closing odds or result)"
            rows.append(row)
            continue
        idx = outcome_index(res["score90"])
        p = devig_shin(close["odds"])
        by_label = dict(zip(close["outcomes"], p))
        market_p = [by_label[o] for o in ["home", "draw", "away"]]
        pv = entry["final"]["payload"]["probs_90"]
        mv_final = [pv["home"], pv["draw"], pv["away"]]
        p1 = entry["v1"]["payload"]["probs_90"]
        mv_v1 = [p1["home"], p1["draw"], p1["away"]]
        row.update({
            "status": "scored",
            "close_book": close["book"],
            "outcome": ["home", "draw", "away"][idx],
            "model_rps": rps(mv_final, idx),
            "model_rps_v1": rps(mv_v1, idx),
            "market_rps": rps(market_p, idx),
            "model_brier": brier(mv_final, idx),
            "market_brier": brier(market_p, idx),
            "model_better": rps(mv_final, idx) < rps(market_p, idx),
        })
        j = entry["final"]["payload"].get("j")
        if j and row["contaminated_j"]:
            j = None
        if j:
            jp = j["probs_90"]
            jv = [jp["home"], jp["draw"], jp["away"]]
            row["j_rps"] = rps(jv, idx)
            row["j_brier"] = brier(jv, idx)
            row["j_better"] = rps(jv, idx) < rps(market_p, idx)
            row["j_deviated"] = any(abs(a - b) > 1e-9
                                    for a, b in zip(jv, mv_final))
        rows.append(row)
    return rows


# --- the bit-identical recompute gate ---------------------------------------

NUMERIC_KEYS = ["model_rps", "model_rps_v1", "market_rps", "model_brier",
                "market_brier", "j_rps", "j_brier"]
FLAG_KEYS = ["status", "contaminated", "model_better", "j_better",
             "close_book", "outcome", "pilot"]


def verify_against_v2(rows=None, report_path=V2_REPORT):
    """Compare recomputed rows against
    wc2026/history/falsification-2026/report.json. Returns a list of
    mismatch strings — MUST be empty (Migration Phase 1 acceptance)."""
    if not report_path.exists():
        return [f"missing v2 report at {report_path}"]
    v2 = {r["match_id"]: r
          for r in json.loads(report_path.read_text(encoding="utf-8"))["b1"]}
    rows = score_rows() if rows is None else rows
    mine = {r["match_id"]: r for r in rows}
    problems = []
    if set(v2) != set(mine):
        problems.append(f"match set differs: only-v2={sorted(set(v2) - set(mine))} "
                        f"only-store={sorted(set(mine) - set(v2))}")
    for mid in sorted(set(v2) & set(mine)):
        a, b = v2[mid], mine[mid]
        for k in NUMERIC_KEYS:
            if (k in a) != (k in b):
                problems.append(f"{mid}.{k}: presence differs")
            elif k in a and abs(a[k] - b[k]) > 1e-12:
                problems.append(f"{mid}.{k}: {a[k]!r} != {b[k]!r}")
        for k in FLAG_KEYS:
            if a.get(k) != b.get(k):
                problems.append(f"{mid}.{k}: {a.get(k)!r} != {b.get(k)!r}")
    return problems


# --- stratified analysis -----------------------------------------------------

def _paired(rows, key):
    """Per-match paired deltas market_rps - key (positive = instrument
    beat the market on that match)."""
    return [r["market_rps"] - r[key] for r in rows]


def _bootstrap_ci(deltas, n=BOOTSTRAP_N, seed=BOOTSTRAP_SEED, alpha=0.05):
    if not deltas:
        return None
    rng = random.Random(seed)
    means = []
    k = len(deltas)
    for _ in range(n):
        means.append(sum(rng.choice(deltas) for _ in range(k)) / k)
    means.sort()
    lo = means[int(alpha / 2 * n)]
    hi = means[int((1 - alpha / 2) * n) - 1]
    return {"mean": sum(deltas) / k, "ci95": [lo, hi], "n": k,
            "positive": sum(1 for d in deltas if d > 0)}


def _block(rows, key, better_key):
    scored = [r for r in rows if key in r]
    if not scored:
        return {"n": 0}
    return {
        "n": len(scored),
        "mean_rps": sum(r[key] for r in scored) / len(scored),
        "mean_market_rps": sum(r["market_rps"] for r in scored) / len(scored),
        "better_n": sum(1 for r in scored if r[better_key]),
        "paired_delta": _bootstrap_ci(_paired(scored, key)),
    }


def summarize(era="v2-wc2026"):
    """Stratified B1 (exploratory analysis layer — the section-4 verdict
    machinery stays in wc2026/score.py until cutover)."""
    rows = [r for r in score_rows(era) if r.get("status") == "scored"]
    main = [r for r in rows if not r["pilot"]]
    pilot = [r for r in rows if r["pilot"]]
    j_rows = [r for r in main if "j_rps" in r]
    return {
        "era": era,
        "main_M": _block(main, "model_rps", "model_better"),
        "main_J": _block(j_rows, "j_rps", "j_better"),
        "main_J_deviated": _block(
            [r for r in j_rows if r.get("j_deviated")], "j_rps", "j_better"),
        "main_J_equals_M": _block(
            [r for r in j_rows if not r.get("j_deviated")], "j_rps",
            "j_better"),
        "pilot_M": _block(pilot, "model_rps", "model_better"),
        "note": ("J-deviated stratum is the hypothesis' real test point "
                 "(review section 3.2); J=M matches carry no information "
                 "about J. Tiny n -> wide CIs is the honest output."),
    }


def _selfcheck():
    fails = []
    # synthetic hand-check: one match, known numbers
    with_store = store_io.STORE_DIR
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            lock_doc = {
                "locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
                "payload": {
                    "match_id": "m_syn", "pilot": False,
                    "kickoff_utc": "2026-01-03T00:00:00+00:00",
                    "probs_90": {"home": 0.5, "draw": 0.3, "away": 0.2},
                    "j": {"probs_90": {"home": 0.6, "draw": 0.25,
                                       "away": 0.15}},
                }, "sha256": "x"}
            store_io.rebuild_era("locks", "syn", [
                {"data": lock_doc, "match_id": "m_syn", "version": 1}])
            store_io.rebuild_era("odds", "syn", [
                {"data": {"match_id": "m_syn", "book": "pinnacle",
                          "market": "1x2", "line": None,
                          "outcomes": ["home", "draw", "away"],
                          "odds": [2.0, 3.0, 6.0], "closing": True,
                          "fetched_at": "2026-01-02T00:00:00+00:00"},
                 "match_id": "m_syn"}])
            store_io.rebuild_era("results", "syn", [
                {"data": {"match_id": "m_syn", "score90": [1, 0]}}])
            rows = score_rows("syn")
            r = rows[0]
            if r["status"] != "scored":
                fails.append(f"synthetic match unscored: {r}")
            else:
                if abs(r["model_rps"] - rps([0.5, 0.3, 0.2], 0)) > 1e-12:
                    fails.append("synthetic model_rps mismatch")
                if not r.get("j_deviated"):
                    fails.append("synthetic j_deviated should be True")
                if r["contaminated"] or r["contaminated_j"]:
                    fails.append("synthetic match should be clean")
            s = summarize("syn")
            if s["main_J_deviated"]["n"] != 1:
                fails.append("stratification lost the deviated-J row")
        finally:
            store_io.STORE_DIR = with_store
    # deterministic bootstrap
    a = _bootstrap_ci([0.1, -0.05, 0.2])
    b = _bootstrap_ci([0.1, -0.05, 0.2])
    if a != b:
        fails.append("bootstrap must be deterministic (seeded)")
    return fails


def main():
    rows = score_rows()
    problems = verify_against_v2(rows)
    print("=== bit-identical recompute vs wc2026/history/falsification-2026/report.json ===")
    if problems:
        for p in problems:
            print(f"  MISMATCH {p}")
    else:
        scored = sum(1 for r in rows if r.get("status") == "scored")
        print(f"  OK — {len(rows)} matches, {scored} scored, every shared "
              f"field identical to the v2 report")
    print("\n=== stratified B1 (exploratory) ===")
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
