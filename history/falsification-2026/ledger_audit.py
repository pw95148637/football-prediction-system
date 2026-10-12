"""Mechanical cross-check: does the prose (verdicts_main.md / pilot_verdicts.md)
agree with the structured ledgers (real_bets.jsonl, mechanism_ledger.jsonl)?

Why this exists: on 2026-07-17 a real-money bet on one match was
narrated in verdicts_main.md as a live position for days, but never made it
into real_bets.jsonl -- the only way this surfaced was a full manual re-read
of both files by an LLM, which is exactly the failure mode this project's own
CLAUDE.md flags (relying on an LLM to remember/re-derive something a script
could just check, burning context and risking a miss). This script replaces
that re-read with a few minutes of deterministic text-scanning. It is a
REPORTING tool, not a hard gate wired into selftest.py's pass/fail: prose is
human-written and evolves, so false positives are expected (a section may
disclose a real-money bet without using the literal word this script scans
for) -- read the output, don't treat a non-empty report as a build failure.

Run: python wc2026/ledger_audit.py
Recommended: run once per session alongside `python -m football_ai.core.agenda`
(the existing session-open check) whenever verdicts_main.md/pilot_verdicts.md
or a ledger file changed since the last run -- not on every invocation.

Checks:
  1. real-money mention -> ledger entry:      every "真錢"/"real money" match
     section in the verdict files should have >=1 real_bets.jsonl row for
     that match_id.
  2. ledger entry -> real-money disclosure:   every real_bets.jsonl match_id
     should have a "真錢"/"real money" mention somewhere in that match's
     verdict section (catches the opposite gap: a logged bet nobody wrote
     up in the narrative, for example a bet logged after the fact).
  3. J exists -> mechanism logged:            every locks/*.json or *.v2.json
     whose payload carries a "j" key should have a mechanism_ledger.jsonl
     entry for that match_id (catches tag-and-forget, e.g. m103/m104 before
     2026-07-17).
  4. stale checkbox in a settled section:     a "- [ ] " line inside a match
     section that also contains a settlement marker ("Phase 4 結算" / "賽果"
     / "賽後結算") is very likely stale, not a live TODO -- flagged for a
     human/LLM glance, not auto-fixed (some genuinely are still open).
"""
import json
import re
from pathlib import Path

BASE = Path(__file__).parent
VERDICT_FILES = ["verdicts_main.md", "pilot_verdicts.md"]
LOCKS_DIR = BASE / "locks"
REAL_BETS = BASE / "real_bets.jsonl"
MECH_LEDGER = BASE / "mechanism_ledger.jsonl"

SECTION_RE = re.compile(r"^##\s+(?:M|m|r16_|r32_)\S*", re.MULTILINE)
REAL_MONEY_RE = re.compile(r"真錢|real money", re.IGNORECASE)
SETTLED_RE = re.compile(r"Phase 4 結算|賽果|賽後結算")
CHECKBOX_OPEN_RE = re.compile(r"^- \[ \] .+$", re.MULTILINE)

# A section header doesn't always carry the bare match_id verbatim (e.g.
# "## m94 美國 – 比利時" vs match_id "m94_usa_bel") -- match on the numeric/
# special-case prefix only (m94, r16_por_esp, r32_aus_egy, ...), which is
# what every match_id in this project's ledgers is keyed by.
MATCH_PREFIX_RE = re.compile(r"^(m\d+|r16_\w+|r32_\w+)", re.IGNORECASE)


def _load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _sections(text):
    """Split a verdict file into (match_prefix, section_text) chunks, one
    per '## ...' heading. match_prefix is lowercased (m94, r16_por_esp, ...)."""
    starts = [m.start() for m in SECTION_RE.finditer(text)] + [len(text)]
    out = []
    for i in range(len(starts) - 1):
        chunk = text[starts[i]:starts[i + 1]]
        header_line = chunk.splitlines()[0]
        pm = MATCH_PREFIX_RE.search(header_line.lstrip("#").strip())
        if pm:
            out.append((pm.group(1).lower(), chunk))
    return out


def check_real_money_vs_ledger():
    """Checks 1 and 2: prose <-> real_bets.jsonl agreement."""
    findings = []
    real_bets = _load_jsonl(REAL_BETS)
    ledger_match_ids = {b["match_id"].lower() for b in real_bets}
    # match_id in the ledger is the full id (m98_esp_bel); sections key off
    # the short prefix (m98) -- build a prefix set too.
    ledger_prefixes = {MATCH_PREFIX_RE.match(mid).group(1) for mid in ledger_match_ids
                        if MATCH_PREFIX_RE.match(mid)}

    prose_prefixes_with_real_money = set()
    all_prose_prefixes = set()
    for fname in VERDICT_FILES:
        path = BASE / fname
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for prefix, chunk in _sections(text):
            all_prose_prefixes.add(prefix)
            if REAL_MONEY_RE.search(chunk):
                prose_prefixes_with_real_money.add(prefix)
                if prefix not in ledger_prefixes:
                    findings.append(
                        f"[real-money mentioned, no ledger entry] {fname}: "
                        f"section '{prefix}' mentions 真錢/real money but no "
                        f"real_bets.jsonl row starts with match_id '{prefix}'")

    for prefix in ledger_prefixes:
        if prefix.startswith("r32_"):
            # pilot_verdicts.md's three R32 pilot-phase matches are titled
            # by Chinese team names ("## Settle:澳洲 1-1 埃及...") not by
            # match_id prefix -- SECTION_RE/MATCH_PREFIX_RE structurally
            # can't find them. Manually verified 2026-07-17 all three are
            # correctly documented; the pilot phase is closed and this
            # header convention won't change, so this is a known, accepted
            # blind spot for this specific prefix family, not a live bug.
            continue
        if prefix not in all_prose_prefixes:
            findings.append(
                f"[ledger entry, no matching section found] real_bets.jsonl "
                f"has match_id prefix '{prefix}' but no '## {prefix}...' "
                f"section header was found in {VERDICT_FILES}")
        elif prefix not in prose_prefixes_with_real_money:
            findings.append(
                f"[ledger entry, no real-money disclosure in prose] "
                f"real_bets.jsonl has match_id prefix '{prefix}' but its "
                f"verdict section never mentions 真錢/real money -- add a "
                f"disclosure paragraph (see m101/m102 for the pattern)")
    return findings


def check_mechanism_coverage():
    """Check 3: every locked J should have a mechanism_ledger.jsonl row."""
    findings = []
    mech_ids = {m["match_id"] for m in _load_jsonl(MECH_LEDGER)}
    if not LOCKS_DIR.exists():
        return findings
    for lock_path in sorted(LOCKS_DIR.glob("*.json")):
        try:
            doc = json.loads(lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        payload = doc.get("payload", {})
        if "j" not in payload:
            continue
        match_id = payload.get("match_id") or lock_path.stem.split(".v")[0]
        if match_id not in mech_ids:
            findings.append(
                f"[J locked, no mechanism_ledger.jsonl entry] {lock_path.name} "
                f"carries a 'j' field for match_id '{match_id}' but "
                f"mechanism_ledger.jsonl has no row for it")
    return findings


def check_stale_checkboxes():
    """Check 4: unchecked TODO boxes inside an already-settled section."""
    findings = []
    for fname in VERDICT_FILES:
        path = BASE / fname
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for prefix, chunk in _sections(text):
            if not SETTLED_RE.search(chunk):
                continue
            opens = CHECKBOX_OPEN_RE.findall(chunk)
            if opens:
                findings.append(
                    f"[possibly-stale checkbox] {fname} section '{prefix}' "
                    f"looks settled but has {len(opens)} unchecked box(es): "
                    f"{opens[0][:70]}{'...' if len(opens[0]) > 70 else ''}")
    return findings


def run():
    all_findings = (
        check_real_money_vs_ledger()
        + check_mechanism_coverage()
        + check_stale_checkboxes()
    )
    if not all_findings:
        print("ledger_audit: clean -- no drift found between prose and ledgers")
        return 0
    print(f"ledger_audit: {len(all_findings)} finding(s) -- REVIEW, don't auto-trust, prose false-positives expected:\n")
    for f in all_findings:
        print(f"  - {f}")
    return 0  # reporting tool, not a hard gate -- exit 0 regardless


def _selfcheck():
    """Planted-fault checks against throwaway temp files -- never touches
    the real wc2026/ files. Returns a list of failure strings (empty=pass)."""
    import tempfile
    fails = []

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "locks").mkdir()
        verdict = td / "verdicts_main.md"
        real_bets = td / "real_bets.jsonl"
        mech = td / "mechanism_ledger.jsonl"

        # Fault A: real-money mentioned in prose, no ledger row.
        verdict.write_text(
            "## m201 A vs B\n真錢揭露:使用者下了 100。\n\n## m202 C vs D\n沒有下注。\n",
            encoding="utf-8")
        real_bets.write_text("", encoding="utf-8")
        mech.write_text("", encoding="utf-8")

        import importlib
        global BASE, VERDICT_FILES, REAL_BETS, MECH_LEDGER, LOCKS_DIR
        orig = (BASE, VERDICT_FILES, REAL_BETS, MECH_LEDGER, LOCKS_DIR)
        BASE, VERDICT_FILES, REAL_BETS, MECH_LEDGER, LOCKS_DIR = (
            td, ["verdicts_main.md"], real_bets, mech, td / "locks")
        try:
            findings = check_real_money_vs_ledger()
            if not any("m201" in f and "no ledger entry" in f for f in findings):
                fails.append("fault A (real-money mention with no ledger row) not caught")

            # Fault B: ledger row, no matching prose section at all.
            real_bets.write_text(
                json.dumps({"match_id": "m999_zzz", "selection": "x", "stake": 1, "result": "win"}) + "\n",
                encoding="utf-8")
            findings = check_real_money_vs_ledger()
            if not any("m999" in f and "no matching section" in f for f in findings):
                fails.append("fault B (ledger row with no prose section) not caught")

            # Fault C: ledger row + section exists, but section never says 真錢.
            real_bets.write_text(
                json.dumps({"match_id": "m202_cd", "selection": "x", "stake": 1, "result": "win"}) + "\n",
                encoding="utf-8")
            findings = check_real_money_vs_ledger()
            if not any("m202" in f and "no real-money disclosure" in f for f in findings):
                fails.append("fault C (ledger row, prose exists but never discloses) not caught")

            # Fault D: clean case should report nothing.
            verdict.write_text("## m203 E vs F\n真錢揭露:下了 50。\n", encoding="utf-8")
            real_bets.write_text(
                json.dumps({"match_id": "m203_ef", "selection": "x", "stake": 1, "result": "win"}) + "\n",
                encoding="utf-8")
            findings = check_real_money_vs_ledger()
            if findings:
                fails.append(f"clean case should report nothing, got {findings}")

            # Fault E: J locked, no mechanism entry.
            LOCKS_DIR.mkdir(exist_ok=True)
            (LOCKS_DIR / "m204_gh.json").write_text(
                json.dumps({"payload": {"match_id": "m204_gh", "j": {}}}), encoding="utf-8")
            mech.write_text("", encoding="utf-8")
            findings = check_mechanism_coverage()
            if not any("m204_gh" in f for f in findings):
                fails.append("fault E (J locked, no mechanism entry) not caught")

            # Fault F: stale checkbox in a settled section.
            verdict.write_text(
                "## m205 I vs J\n賽果:1-0。\n- [ ] 收盤截圖\n\n## m206 K vs L\n"
                "還沒開球。\n- [ ] 收盤截圖\n", encoding="utf-8")
            findings = check_stale_checkboxes()
            if not any("m205" in f for f in findings):
                fails.append("fault F (stale checkbox in settled section) not caught")
            if any("m206" in f for f in findings):
                fails.append("false positive: m206 is not settled, should not be flagged")
        finally:
            BASE, VERDICT_FILES, REAL_BETS, MECH_LEDGER, LOCKS_DIR = orig

    return fails


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        result = _selfcheck()
        print("OK" if not result else f"FAILURES: {result}")
        sys.exit(0 if not result else 1)
    sys.exit(run())
