"""Single source of truth for where the archived wc2026 v2 falsification
experiment lives on disk.

WHY THIS EXISTS: before the 2026-07-20 archive move ("刀二", git 3c7fcc2),
five football_ai modules — data/import_v2.py,
models/m_dc.py, decision/report.py, research/scoring.py, evidence/schema.py
— each independently computed the wc2026 root via their own
`Path(__file__).parents[N]` arithmetic. That worked while the v2 files
lived at the wc2026/ root, but it meant "where is the archive" was answered
in five separate places; any future relocation (as just happened) required
re-deriving and re-verifying the parents[N] count in each of them
separately. This module answers that question exactly once — the five
modules now import v2_archive_dir() instead of computing their own
Path(__file__).parents[N].

The archive is a MIRROR of the pre-archive wc2026/ root (git 3c7fcc2):
locks/, odds/, data/, results.json, bets.jsonl,
..., and every v2-era *.py script (lock.py, score.py, model.py, firewall.py,
...) all live directly under this directory, at the same level — there is
no v2-pipeline/ sublayer. That is precisely why the archived scripts (which
locate their own sibling data via `Path(__file__).parent`) needed ZERO
edits when they moved: their own directory IS the archive root, exactly as
before the move it was the wc2026 root.
"""
from pathlib import Path


def v2_archive_dir() -> Path:
    """Absolute path of wc2026/history/falsification-2026 — the frozen,
    read-only archive of the 2026 WC falsification experiment (locks/,
    odds/, data/, results.json, bets.jsonl, ..., and the v2-era *.py
    instruments including firewall.py, all at this directory's top level,
    mirroring the old wc2026/ root layout)."""
    return Path(__file__).resolve().parents[2] / "history" / "falsification-2026"


def repo_root() -> Path:
    """Absolute path of the wc2026 repo root. Competition-agnostic, unlike
    `v2_archive_dir()` above (audit finding, research/audit/final_report.md
    section 4.9: that function is hardcoded to one specific archive with no
    parameter axis) -- new, non-WC2026 data sources should build their paths
    from this, not from a copy-pasted `parents[N]` chain of their own."""
    return Path(__file__).resolve().parents[2]


def raw_cache_dir(source_name: str) -> Path:
    """Where a data-source adapter caches its raw fetched files, e.g.
    `raw_cache_dir("football_data_co_uk")`. Gitignored (see .gitignore) --
    bulky and re-fetchable, same discipline as the archived experiment's
    odds/raw/ (final_report.md notes that convention approvingly)."""
    return repo_root() / "football_ai" / "data" / "raw" / source_name


def _selfcheck():
    fails = []
    root = repo_root()
    if not (root / "football_ai").is_dir():
        fails.append(f"repo_root() {root} does not contain football_ai/ -- wrong parents[] depth")
    if not (root / ".git").exists():
        fails.append(f"repo_root() {root} does not look like the repo root (no .git)")
    v2 = v2_archive_dir()
    if v2 != root / "history" / "falsification-2026":
        fails.append(f"v2_archive_dir() drifted from repo_root()-relative expectation: {v2}")
    cache = raw_cache_dir("some_source")
    if cache != root / "football_ai" / "data" / "raw" / "some_source":
        fails.append(f"raw_cache_dir() shape unexpected: {cache}")
    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
