"""Package-wide selftest: every module's planted-fault checks + the two
structural gates (import direction, bit-identical v2 recompute).

Same doctrine as the main tool's grader.py and wc2026/selftest.py: graders
point at ENGINEERING HONESTY (planted faults must be caught, structure must
hold), never at prediction performance. Run from wc2026/:

    python -m football_ai.audit.selftest            # everything
    python -m football_ai.audit.selftest --fast     # skip the store rebuild

Exit 0 = all pass.
"""
import re
import sys
from pathlib import Path

PKG = Path(__file__).parents[1]

# module path -> forbidden import prefixes (README dependency direction)
IMPORT_RULES = {
    "research": ["football_ai.market.consensus", "football_ai.decision",
                 "football_ai.evaluation"],
    "market": ["football_ai.decision", "football_ai.research",
               "football_ai.evaluation"],
    "evidence": ["football_ai.research", "football_ai.decision",
                 "football_ai.market"],
    "models": ["football_ai.research", "football_ai.decision",
               "football_ai.market.consensus"],
    "core": ["football_ai.research", "football_ai.decision",
             "football_ai.market", "football_ai.evaluation",
             "football_ai.models", "football_ai.data"],
    "evaluation": ["football_ai.research", "football_ai.decision"],
    "store": ["football_ai."],  # store imports nothing from the package
}


def imported_modules(text):
    """Every football_ai module a source text imports, as full dotted names.

    Parsed with `ast`, not a regex: `from football_ai.market import consensus`
    imports football_ai.market.consensus, and a regex that only reads the
    module part (football_ai.market) cannot see that. Function-level imports
    count (they run just the same)."""
    import ast
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("football_ai"):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("football_ai"):
            found.append(node.module)
            for alias in node.names:
                found.append(f"{node.module}.{alias.name}")
    return found


def forbidden_in(text, forbidden):
    return sorted({m for m in imported_modules(text)
                   for bad in forbidden
                   if m == bad or m.startswith(bad + ".")
                   or (bad.endswith(".") and m.startswith(bad))})


def check_import_direction():
    fails = []
    # the detector must catch every spelling before it is trusted
    planted = {
        "from football_ai.market.consensus import x": ["football_ai.market.consensus"],
        "from football_ai.market import consensus": ["football_ai.market.consensus"],
        "import football_ai.market.consensus": ["football_ai.market.consensus"],
        "from football_ai import decision": ["football_ai.decision"],
        "def f():\n    from football_ai.decision.sop import evaluate": ["football_ai.decision"],
    }
    rule = ["football_ai.market.consensus", "football_ai.decision"]
    for src, _ in planted.items():
        if not forbidden_in(src, rule):
            fails.append(f"import checker missed: {src!r}")
    if forbidden_in("from football_ai.market import devig", rule):
        fails.append("import checker flagged an allowed import (market.devig)")
    for subdir, forbidden in IMPORT_RULES.items():
        for f in (PKG / subdir).glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for mod in forbidden_in(text, forbidden):
                fails.append(
                    f"{subdir}/{f.name} imports {mod} — violates "
                    f"the declared dependency direction (README)")
    return fails


def run_selfchecks():
    """Every module's _selfcheck(). A module that fails to import is a
    failure, not a skip — the package must always be loadable."""
    fails = []
    modules = [
        "football_ai.store.io",
        "football_ai.store.sync",
        "football_ai.core.states",
        "football_ai.core.paths",
        "football_ai.core.competition",
        "football_ai.core.registry",
        "football_ai.core.agenda",
        "football_ai.data.import_v2",
        "football_ai.data.backfill_phase2",
        "football_ai.data.football_data_co_uk",
        "football_ai.data.the_odds_api",
        "football_ai.research.metrics",
        "football_ai.research.scoring",
        "football_ai.research.j_runner",
        "football_ai.market.consensus",
        "football_ai.market.clv",
        "football_ai.evidence.firewall",
        "football_ai.evidence.schema",
        "football_ai.decision.sop",
        "football_ai.decision.sizing",
        "football_ai.decision.judgment",
        "football_ai.decision.track_record",
        "football_ai.data.absence_baserate",
        "football_ai.decision.report",
        "football_ai.audit.experiments",
        "football_ai.evaluation.trading_eval",
        "football_ai.models.m_dc",
        "football_ai.models.division_elo",
        "football_ai.competitions.epl_2026_27",
        "football_ai.competitions.epl_2026_27_fixtures",
        "football_ai.competitions.efl_championship_2026_27",
        "football_ai.competitions.matchflow",
        "football_ai.competitions.schedule",
        "football_ai.competitions.ops_tools",
        "football_ai.competitions.summary",
        "football_ai.competitions.ops",
        "football_ai.competitions",
    ]
    import importlib
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except Exception as e:
            fails.append(f"{name}: import failed — {e}")
            continue
        selfcheck = getattr(mod, "_selfcheck", None)
        if selfcheck is None:
            fails.append(f"{name}: missing _selfcheck() — every module "
                         f"carries its own planted-fault checks")
            continue
        try:
            for msg in selfcheck():
                fails.append(f"{name}: {msg}")
        except Exception as e:
            fails.append(f"{name}: _selfcheck crashed — {e}")
    return fails


def check_recompute(rebuild=True):
    """Migration Phase 1 acceptance: import v2 -> recompute B1 -> must be
    bit-identical to wc2026/report.json."""
    from football_ai.data import import_v2
    from football_ai.research import scoring
    from football_ai.store import io as store_io
    if not rebuild:
        return [f"recompute: {p}" for p in scoring.verify_against_v2()]
    # The rebuild rewrites a whole era of the store, so it runs on a
    # throwaway COPY: a test must never be able to touch the production
    # ledger (CLAUDE.md, append-only records). Until 2026-10-06 it ran on the real one
    # and, with a later era appended, silently reordered hundreds of rows.
    import shutil
    import tempfile
    from pathlib import Path
    real = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "data"
        shutil.copytree(real, copy)
        store_io.STORE_DIR = copy
        try:
            import_v2.import_all()
            problems = scoring.verify_against_v2()
        finally:
            store_io.STORE_DIR = real
    return [f"recompute: {p}" for p in problems]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    fast = "--fast" in argv
    fails = []
    print("import direction:")
    f = check_import_direction()
    print(f"  {'ok' if not f else 'FAIL'} ({len(f)} violations)")
    fails += f
    print("module selfchecks:")
    f = run_selfchecks()
    print(f"  {'ok' if not f else 'FAIL'} ({len(f)} failures)")
    fails += f
    print("bit-identical v2 recompute:")
    f = check_recompute(rebuild=not fast)
    print(f"  {'ok' if not f else 'FAIL'} ({len(f)} mismatches)")
    fails += f
    print()
    if fails:
        for msg in fails:
            print(f"FAIL  {msg}")
        print(f"\n{len(fails)} FAILURES")
        raise SystemExit(1)
    print("ALL FOOTBALL_AI SELFTESTS PASS")


if __name__ == "__main__":
    main()
