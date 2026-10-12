"""Idempotent, era-tagged importer of the wc2026 v2 files into the store.

The v2 files (locks/, odds/, results.json, bets.jsonl, real_bets.jsonl,
mechanism_ledger.jsonl, calibration.json) are, and stay, the ONLY authority
for the live WC2026 falsification experiment. Nothing in this module edits
those files. What this module builds is a DERIVED VIEW inside the store
(era "v2-wc2026") so the rest of football_ai can query the experiment
through one uniform interface (store/io.py) instead of every module
re-learning v2's file layout.

Idempotency is the whole point: this is meant to be re-run any time (a new
match locks, a new odds snapshot lands, a result is entered) without ever
producing duplicate or stale records. That is why every import_* function
below calls `io.rebuild_era(kind, ERA, records)` — an atomic full replace of
that (kind, era)'s records — rather than `io.append`. Append would double
every record on a second run; rebuild_era makes re-running a no-op on
unchanged source data and a clean update when source data changed.

Byte-preservation: for locks in particular, `data` is the full original
lock document, unmodified, so anything downstream that needs the exact
locked payload (probs, rationale, sha256, ...) can trust the store copy
as much as the file on disk. We only ever *add* fields alongside the
original doc (source_path, match_id, version), never mutate it.

odds/raw/ is deliberately skipped: those are the raw upstream API response
dumps (the-odds-api JSON blobs), kept on disk as-is for provenance/replay.
They are not one-record-per-market-line data and importing them would mix
a different shape into the "odds" kind, so they stay out of the store.
"""
import json
import sys
from pathlib import Path

from football_ai.core.paths import v2_archive_dir
from football_ai.store import io

# The archived wc2026 v2 directory (locks/, odds/, results.json, bets.jsonl,
# ...) — see football_ai/core/paths.py for why this is one shared function
# instead of independently-computed Path(__file__).parents[N] arithmetic.
# Kept as a module global (not a constant baked into each function) so
# _selfcheck can monkeypatch it to point at a throwaway fake v2 tree.
V2_DIR = v2_archive_dir()

ERA = "v2-wc2026"


def _rel(path):
    """Path relative to V2_DIR, forward-slashed, for a stable source_path."""
    return path.relative_to(V2_DIR).as_posix()


def _missing(label, path):
    print(f"[import_v2] note: {label} not found at {path} — importing 0")


def import_locks():
    """One store record per lock FILE (including .vN amends).

    data = the full original lock doc, byte-preserved. Extra fields:
    source_path, match_id, version (all read out of the doc, not invented).
    """
    locks_dir = V2_DIR / "locks"
    if not locks_dir.is_dir():
        _missing("locks/", locks_dir)
        return io.rebuild_era("locks", ERA, [])

    records = []
    for path in sorted(locks_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        payload = doc.get("payload", {})
        records.append({
            "data": doc,
            "source_path": _rel(path),
            "match_id": payload.get("match_id"),
            "version": doc.get("version"),
        })
    return io.rebuild_era("locks", ERA, records)


def import_odds():
    """One store record per odds/*.jsonl LINE. odds/raw/ is skipped on
    purpose — see module docstring."""
    odds_dir = V2_DIR / "odds"
    if not odds_dir.is_dir():
        _missing("odds/", odds_dir)
        return io.rebuild_era("odds", ERA, [])

    records = []
    for path in sorted(odds_dir.glob("*.jsonl")):
        rel = _rel(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            records.append({
                "data": row,
                "source_path": rel,
                "match_id": row.get("match_id"),
            })
    return io.rebuild_era("odds", ERA, records)


def import_results():
    """One record per match key in results.json."""
    path = V2_DIR / "results.json"
    if not path.exists():
        _missing("results.json", path)
        return io.rebuild_era("results", ERA, [])

    doc = json.loads(path.read_text(encoding="utf-8"))
    rel = _rel(path)
    records = [
        {"data": {"match_id": k, **v}, "source_path": rel, "match_id": k}
        for k, v in doc.items()
    ]
    return io.rebuild_era("results", ERA, records)


def _import_jsonl_file(kind, filename):
    """Shared logic for the flat one-line-per-record top-level jsonl files
    (bets.jsonl, real_bets.jsonl, mechanism_ledger.jsonl)."""
    path = V2_DIR / filename
    if not path.exists():
        _missing(filename, path)
        return io.rebuild_era(kind, ERA, [])

    rel = _rel(path)
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        records.append({"data": row, "source_path": rel})
    return io.rebuild_era(kind, ERA, records)


def import_bets():
    return _import_jsonl_file("bets", "bets.jsonl")


def import_real_bets():
    return _import_jsonl_file("real_bets", "real_bets.jsonl")


def import_mechanisms():
    return _import_jsonl_file("mechanisms", "mechanism_ledger.jsonl")


def import_calibration():
    """One record: the whole calibration.json dict."""
    path = V2_DIR / "calibration.json"
    if not path.exists():
        _missing("calibration.json", path)
        return io.rebuild_era("calibrations", ERA, [])

    doc = json.loads(path.read_text(encoding="utf-8"))
    records = [{"data": doc, "source_path": _rel(path)}]
    return io.rebuild_era("calibrations", ERA, records)


def import_all():
    """Run every importer. Returns {kind: count}. Never raises on a missing
    source file (a fresh checkout may not have bets.jsonl yet, say) — each
    import_* function already degrades to 0 records for that kind."""
    return {
        "locks": import_locks(),
        "odds": import_odds(),
        "results": import_results(),
        "bets": import_bets(),
        "real_bets": import_real_bets(),
        "mechanisms": import_mechanisms(),
        "calibrations": import_calibration(),
    }


def _selfcheck():
    """Planted-fault checks against a fake, throwaway v2 tree — never
    touches the real wc2026/ files. Returns a list of failure strings
    (empty = pass)."""
    import tempfile

    global V2_DIR
    old_v2_dir = V2_DIR
    old_store_dir = io.STORE_DIR
    fails = []

    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "wc2026"
        (fake / "locks").mkdir(parents=True)
        (fake / "odds" / "raw").mkdir(parents=True)

        # One base lock + one .v2 amend for the same match.
        lock_v1 = {
            "locked_at": "2026-01-01T00:00:00+00:00",
            "version": 1,
            "payload": {
                "match_id": "m99_fake_team",
                "pilot": False,
                "teams": {"home": "Fakeland", "away": "Otherland"},
                "kickoff_utc": "2026-01-02T00:00:00+00:00",
            },
            "sha256": "abc123",
        }
        lock_v2 = {
            "locked_at": "2026-01-01T12:00:00+00:00",
            "version": 2,
            "amend_reason": "planted test amend",
            "payload": {
                "match_id": "m99_fake_team",
                "pilot": False,
                "teams": {"home": "Fakeland", "away": "Otherland"},
                "kickoff_utc": "2026-01-02T00:00:00+00:00",
                "j": {"probs_90": {"home": 0.4, "draw": 0.3, "away": 0.3}},
            },
            "sha256": "def456",
        }
        (fake / "locks" / "m99_fake_team.json").write_text(
            json.dumps(lock_v1, ensure_ascii=False), encoding="utf-8")
        (fake / "locks" / "m99_fake_team.v2.json").write_text(
            json.dumps(lock_v2, ensure_ascii=False), encoding="utf-8")

        odds_lines = [
            {"match_id": "m99_fake_team", "book": "pinnacle", "market": "1x2",
             "line": None, "outcomes": ["home", "draw", "away"],
             "odds": [2.0, 3.0, 4.0], "source": "test", "fetched_at": "t",
             "closing": False, "note": "n1"},
            {"match_id": "m99_fake_team", "book": "pinnacle", "market": "1x2",
             "line": None, "outcomes": ["home", "draw", "away"],
             "odds": [2.1, 3.1, 4.1], "source": "test", "fetched_at": "t2",
             "closing": True, "note": "n2"},
        ]
        (fake / "odds" / "m99_fake_team.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in odds_lines) + "\n",
            encoding="utf-8")
        # A raw dump that must be skipped.
        (fake / "odds" / "raw" / "20260101T000000Z_dump.json").write_text(
            "{}", encoding="utf-8")

        results_doc = {"m99_fake_team": {"score90": [1, 0], "advanced": "home",
                                          "notes": "test"}}
        (fake / "results.json").write_text(
            json.dumps(results_doc, ensure_ascii=False), encoding="utf-8")

        # bets.jsonl deliberately absent — tests the missing-file path.

        try:
            V2_DIR = fake
            io.STORE_DIR = Path(td) / "store"

            counts1 = import_all()
            if counts1.get("locks") != 2:
                fails.append(f"expected 2 lock records, got {counts1.get('locks')}")
            if counts1.get("odds") != 2:
                fails.append(f"expected 2 odds records, got {counts1.get('odds')}")
            if counts1.get("results") != 1:
                fails.append(f"expected 1 results record, got {counts1.get('results')}")
            if counts1.get("bets") != 0:
                fails.append(f"expected 0 bets records (file absent), got {counts1.get('bets')}")

            counts2 = import_all()
            for kind in counts1:
                if counts1[kind] != counts2[kind]:
                    fails.append(
                        f"re-import changed count for {kind}: {counts1[kind]} -> {counts2[kind]}")

            lock_records = io.read("locks", era=ERA)
            if len(lock_records) != 2:
                fails.append(f"double-import duplicated lock records: {len(lock_records)}")

            v1_stored = next((r for r in lock_records
                               if r["data"].get("version") == 1), None)
            if v1_stored is None:
                fails.append("v1 lock record not found in store")
            elif v1_stored["data"] != lock_v1:
                fails.append("lock v1 doc not preserved byte-for-byte (json round-trip mismatch)")

            odds_records = io.read("odds", era=ERA)
            for rec in odds_records:
                if "raw" in rec.get("source_path", ""):
                    fails.append("odds/raw/ leaked into the store")

        finally:
            V2_DIR = old_v2_dir
            io.STORE_DIR = old_store_dir

    return fails


if __name__ == "__main__":
    counts = import_all()
    for kind, n in counts.items():
        print(f"{kind}: {n}")
    sys.exit(0)
