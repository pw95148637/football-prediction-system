"""Experiment registry (Architecture Review v3 section 4.7 — Governance
Layer): "現行 CHANGELOG 的散文條目補一行結構化記錄
{id, hypothesis, change, metric, result, decision}."

PROTOCOL.md's CHANGELOG stays the authoritative narrative -- this module
is its structured twin, not a replacement: every change to the pipeline
(a new checklist item, a new gap threshold, a new evidence category...)
gets one row here alongside the prose write-up, so later someone can
query "what did we try, and did we keep it" without re-reading the whole
changelog. Stored via store/io.py under kind="experiments", era="v3"
(single-writer rule -- nothing else opens this file for writing).
"""
from ..store import io

# Decisions this registry accepts. "informational" covers an experiment
# run for its own sake (a diagnostic, not a pipeline change proposal) --
# it is neither adopted nor rejected because nothing was being proposed.
DECISIONS = ["adopted", "rejected", "deferred", "informational"]

KIND = "experiments"


def record(exp_id, hypothesis, change, metric, result, decision, notes=""):
    """Append one experiment record. Raises ValueError on an empty
    exp_id/hypothesis/decision, or a decision outside DECISIONS -- an
    experiment with no id can't be found again, and an unrecognized
    decision value would silently break every later query against this
    registry. Returns the full store record as written (see store/io.append).
    """
    if not exp_id or not str(exp_id).strip():
        raise ValueError("exp_id must be a non-empty string")
    if not hypothesis or not str(hypothesis).strip():
        raise ValueError("hypothesis must be a non-empty string")
    if not decision or not str(decision).strip():
        raise ValueError("decision must be a non-empty string")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")

    data = {
        "exp_id": exp_id,
        "hypothesis": hypothesis,
        "change": change,
        "metric": metric,
        "result": result,
        "decision": decision,
        "notes": notes,
    }
    return io.append(KIND, data, era="v3")


def list_experiments():
    """All recorded experiments, in store (append) order, as plain data
    dicts (the store envelope's "data" field, not the full record)."""
    return [rec["data"] for rec in io.read(KIND)]


def find(exp_id):
    """The most recently recorded experiment data dict with this exp_id,
    or None if it was never recorded. Scans in store order and keeps the
    last match, so a later record under the same exp_id (e.g. a decision
    revisited after more evidence) is treated as the current status."""
    match = None
    for data in list_experiments():
        if data.get("exp_id") == exp_id:
            match = data
    return match


def _print_table(rows=None):
    """Print a compact table of experiments to stdout (CLI entry point)."""
    rows = list_experiments() if rows is None else rows
    if not rows:
        print("(no experiments recorded)")
        return
    id_w = max(len("exp_id"), max(len(str(r.get("exp_id", ""))) for r in rows))
    dec_w = max(len("decision"), max(len(str(r.get("decision", ""))) for r in rows))
    header = f"{'exp_id':<{id_w}}  {'decision':<{dec_w}}  hypothesis"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{str(r.get('exp_id', '')):<{id_w}}  {str(r.get('decision', '')):<{dec_w}}  {r.get('hypothesis', '')}")


def _selfcheck():
    """Planted-fault checks, called by audit/selftest. Redirects
    store/io.STORE_DIR to a temp directory for the duration of the check
    (same pattern as store/io.py's own _selfcheck), so this never touches
    the real store/data/experiments.jsonl. Returns a list of failure
    strings (empty = all good)."""
    import tempfile
    from pathlib import Path

    fails = []
    old_dir = io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        io.STORE_DIR = Path(td)
        try:
            record(
                "exp-001",
                "Cluster-aware Kelly reduces overstaked correlated edges",
                "size_cluster() collapses same-cluster bets to one stake",
                "aggregate bankroll fraction staked per correlated cluster",
                "single stake per cluster, as designed",
                "adopted",
                notes="unit test round-trip",
            )
            exps = list_experiments()
            if len(exps) != 1:
                fails.append(f"record/list_experiments roundtrip: expected 1 record, got {len(exps)}")

            found = find("exp-001")
            if found is None or found.get("decision") != "adopted":
                fails.append(f"find('exp-001') should return the recorded data, got {found!r}")

            if find("does-not-exist") is not None:
                fails.append("find() on an unknown exp_id should return None")

            try:
                record("exp-002", "h", "c", "m", "r", "not_a_real_decision")
                fails.append("a bad decision value should raise ValueError")
            except ValueError:
                pass

            try:
                record("", "h", "c", "m", "r", "adopted")
                fails.append("an empty exp_id should raise ValueError")
            except ValueError:
                pass

            try:
                record("exp-003", "", "c", "m", "r", "adopted")
                fails.append("an empty hypothesis should raise ValueError")
            except ValueError:
                pass
        finally:
            io.STORE_DIR = old_dir

    return fails


if __name__ == "__main__":
    _print_table()
