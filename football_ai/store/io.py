"""Single-writer append-only JSONL store (Architecture Review v3 section 4.1).

One JSON object per line: {"era": str, "kind": str, "ts": iso8601, "data": {}}.
Era "v2-wc2026" records are a DERIVED view rebuilt from the canonical v2
files during migration (records carry "source_path"); era "v3" records are
primary. All writes go through this module — nothing else opens store files
for writing (single-writer rule).

rebuild_era() exists for idempotent importers: it atomically replaces every
record of (kind, era) while leaving other eras' lines byte-identical. This
is not mutation of history — during migration the v2 era here is a view;
the v2 source files remain the untouched authority.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STORE_DIR = Path(__file__).parent / "data"

KINDS = [
    "fixtures", "locks", "odds", "results", "bets", "real_bets",
    "mechanisms", "calibrations", "experiments", "evidence",
    "verdict_backfill",
    # 2026-08-26, added deliberately: the 研判 layer's per-match record
    # (its own probabilities, and how they diverge from the locked J --
    # charter section 2 / CLAUDE.md, delivery of 研判 require the divergence be declared, never
    # silently held). The WC2026 window wrote these to
    # history/falsification-2026/judgment_ledger.jsonl, which has been a
    # frozen read-only archive since 2026-07-20; appending a new
    # competition's rows there would be rewriting closed history. Era-
    # tagged here instead. Decision support only: like the file it
    # replaces, it never feeds B1/B2 and never writes back to M or J.
    "judgments",
]


def _path(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown store kind '{kind}' (add it to io.KINDS "
                         f"deliberately, not ad hoc)")
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    return STORE_DIR / f"{kind}.jsonl"


def _now():
    return datetime.now(timezone.utc).isoformat()


def append(kind, data, era="v3", **extra):
    """Append one record. Returns the full record as written."""
    rec = {"era": era, "kind": kind, "ts": _now(), **extra, "data": data}
    # newline="\n": the same bytes on Windows and Linux. Text mode would write
    # CRLF on a Windows machine and LF in a cloud session, so a ledger edited
    # from both would carry mixed line endings (the store files are `-text`
    # in .gitattributes: git never normalises them, whatever is written stays).
    with _path(kind).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read(kind, era=None):
    """All records of a kind, optionally filtered by era, in file order."""
    p = _path(kind)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if era is None or rec.get("era") == era:
            out.append(rec)
    return out


def _identity(rec):
    """Everything about a record EXCEPT when it was written. Two records
    with the same identity are the same fact recorded twice, so the older
    timestamp is the true one."""
    return json.dumps({k: v for k, v in rec.items() if k != "ts"},
                      sort_keys=True, ensure_ascii=False)


def rebuild_era(kind, era, records):
    """Atomically replace all (kind, era) records; other eras' lines are
    preserved byte-identically AND stay where they were. `records` are dicts
    of {"data":..., **extra}; era/kind/ts are stamped here. Returns count
    written.

    Two properties keep the file's git history honest:
      * a rebuild that reproduces what is already there writes NOTHING;
      * a rebuild that changes the era puts its lines where the era's first
        line used to be (or at the end if the era is new), so appending
        another era later and rebuilding this one afterwards does not
        shuffle the file. (The old version always wrote "other eras, then
        this era", which moved a derived era to the end of the file the
        first time anything was appended after it -- 542 lines of odds.jsonl
        showed as rewritten after a plain selftest run, 2026-10-06.)"""
    p = _path(kind)
    lines, era_idx = [], []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if json.loads(line).get("era") == era:
                era_idx.append(len(lines))
            lines.append(line)
    existing = [lines[i] for i in era_idx]
    # Preserve the EXISTING ts for any record whose content is unchanged.
    # rebuild_era stamps a fresh timestamp, so re-running it (the selftest
    # does, every time) rewrote every derived row and made the whole file
    # show as modified in git. On an append-only audit trail that is worse
    # than cosmetic: a real edit hides among hundreds of spurious ones.
    # A derived view that reproduces its source should be BYTE-STABLE.
    prior = {}
    for line in existing:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        prior[_identity(rec)] = rec.get("ts")

    ts = _now()
    new_lines = []
    for r in records:
        extra = {k: v for k, v in r.items() if k != "data"}
        rec = {"era": era, "kind": kind, "ts": ts, **extra, "data": r["data"]}
        rec["ts"] = prior.get(_identity(rec), ts)
        new_lines.append(json.dumps(rec, ensure_ascii=False))
    if new_lines == existing:
        return len(new_lines)
    first = era_idx[0] if era_idx else len(lines)
    era_set = set(era_idx)
    out = []
    for i, line in enumerate(lines):
        if i == first:
            out.extend(new_lines)
        if i not in era_set:
            out.append(line)
    if first == len(lines):
        out.extend(new_lines)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for line in out:
                f.write(line + "\n")
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return len(new_lines)


def _selfcheck():
    """Planted-fault checks, called by audit/selftest."""
    import tempfile as _tf
    global STORE_DIR
    old = STORE_DIR
    fails = []
    with _tf.TemporaryDirectory() as td:
        STORE_DIR = Path(td)
        try:
            append("locks", {"x": 1}, era="v3")
            append("locks", {"y": 2}, era="v2-wc2026", source_path="a.json")
            if len(read("locks")) != 2:
                fails.append("append/read roundtrip")
            if len(read("locks", era="v3")) != 1:
                fails.append("era filter")
            rebuild_era("locks", "v2-wc2026",
                        [{"data": {"y": 3}, "source_path": "a.json"},
                         {"data": {"z": 4}, "source_path": "b.json"}])
            if len(read("locks", era="v2-wc2026")) != 2:
                fails.append("rebuild_era replaces era records")
            if len(read("locks", era="v3")) != 1:
                fails.append("rebuild_era preserves other eras")
            # A rebuild that reproduces the same content must be
            # BYTE-STABLE -- otherwise every selftest run rewrites the
            # derived rows and a real edit hides in the churn.
            same = [{"data": {"y": 3}, "source_path": "a.json"},
                    {"data": {"z": 4}, "source_path": "b.json"}]
            before = _path("locks").read_text(encoding="utf-8")
            rebuild_era("locks", "v2-wc2026", same)
            if _path("locks").read_text(encoding="utf-8") != before:
                fails.append("an identical rebuild must not change a single "
                             "byte; timestamps were being restamped")
            rebuild_era("locks", "v2-wc2026",
                        [{"data": {"y": 99}, "source_path": "a.json"}])
            changed = read("locks", era="v2-wc2026")
            if len(changed) != 1 or changed[0]["data"]["y"] != 99:
                fails.append("a rebuild with CHANGED content must still replace")

            # Another era appended AFTER the derived one: rebuilding the
            # derived era must neither move it nor touch a byte when the
            # content is unchanged, and a changed rebuild keeps its place.
            append("locks", {"later": 1}, era="v3")
            snap = _path("locks").read_text(encoding="utf-8")
            rebuild_era("locks", "v2-wc2026",
                        [{"data": {"y": 99}, "source_path": "a.json"}])
            if _path("locks").read_text(encoding="utf-8") != snap:
                fails.append("an identical rebuild with another era appended "
                             "after it must not reorder or rewrite the file")
            rebuild_era("locks", "v2-wc2026",
                        [{"data": {"y": 7}, "source_path": "a.json"}])
            order = [r["era"] for r in read("locks")]
            if order != ["v2-wc2026", "v3", "v3"] and order != ["v3", "v2-wc2026", "v3"]:
                fails.append(f"a changed rebuild must keep the era in place, "
                             f"got order {order}")

            try:
                append("nope", {})
                fails.append("unknown kind must raise")
            except ValueError:
                pass
        finally:
            STORE_DIR = old
    return fails
