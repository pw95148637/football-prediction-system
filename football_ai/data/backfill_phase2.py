"""Phase 2 structured backfill: narrative verdicts -> structured store fields.

Step 1 of the 2026-07-20 archive pass (git 65e8d15, 3c7fcc2) --
deliberately deferred until the tournament finished (2026-07-20)
so it would only need one pass instead of being redone after every settle.

Lineage of the two source files this module reads (neither is written to;
both are hand-verified narrative extractions, this module only joins and
canonicalizes them):

  - phase2_backfill_draft.json (2026-07-09): a human-verified dry-run
    extraction of 17 matches (12 main + 5 pilot) from verdicts_main.md /
    pilot_verdicts.md, checked line-by-line against source prose. It
    predates m101-m104 (didn't exist yet) and the settle sections for
    m97-m100 (all four kicked off 2026-07-09..12, AFTER the draft's 07-09
    creation date) -- untouched here, used as-is for every field that does
    not change after lock (decision / gap / market_error_mechanism_named).
  - phase2_backfill_final.json (2026-07-20, this backfill's own companion
    artifact): the completion pass -- two_question answered/unanswered
    classification for all 21 matches (never a structured field before),
    a settle-phase refresh for m97-m100 (read fresh from verdicts_main.md's
    own Phase 4 sections, which postdate the draft), full fresh rows for
    m101-m104, and a mechanism_materialization override for the older
    matches whose single free-text draft verdict needs to become a
    per-mechanism-type dict keyed by the ledger's canonical type names.

Two mandatory merge rules (non-negotiable):
  (a) match_id normalization via the fixture registry (football_ai/core/
      registry.py) -- every stored row uses the CANONICAL id (m89 ->
      m89_can_mar, aus_egy -> r32_aus_egy, r16_por_esp stays r16_por_esp
      because that already IS its lock's own id, the registry alias only
      maps it to a match number). _canonicalize() below never hardcodes a
      lookup table -- it always asks the registry/fixtures.
  (b) mechanism_ledger.jsonl is canonical for j_deviation_types /
      j_net_d_eff_shift; the drafts' free-text mechanism descriptions are
      demoted into narrative_note, never used to overwrite the ledger.

Everything that CAN be derived mechanically from the store is joined at
import time here, never hand-transcribed into the JSON source files:
  - settle_outcome.score90 / .advanced   <- store "results" (era v2-wc2026)
  - b1 (model/j/market RPS, *_better, 1x2 outcome label)
                                          <- research.scoring.score_rows()
  - contamination odds_before_lock / post_market_j
                                          <- research.scoring.score_rows()
  - j_deviation_types / j_net_d_eff_shift <- store "mechanisms"
                                             (mechanism_ledger.jsonl import)
Two contamination events cannot be recovered from timestamps at all (they
are self-reported/registered incidents, not timestamp facts) and are
hand-coded below against the PROTOCOL/registry precedent: m97's
leak_to_analyst (firewall self-report, PROTOCOL CHANGELOG 2026-07-07) and
m92's missing (registry.py REGISTERED_MISSING). See core/states.py for the
full TAINT_EVENTS vocabulary this reuses.

Storage: add-only, idempotent, era-tagged -- mirrors import_v2.py's
rebuild_era() pattern exactly, but under its OWN store kind
("verdict_backfill") and its OWN era ("v2-wc2026-backfill") so this never
touches, duplicates, or races with the "locks"/"results"/"mechanisms"
records import_v2.py owns. Re-running this module is always a clean
replace of the whole kind, never an append.
"""
import json
import re
import sys
from pathlib import Path

from football_ai.core import registry
from football_ai.data import import_v2
from football_ai.research import scoring
from football_ai.store import io

PKG_DIR = Path(__file__).parents[1]
DRAFT_PATH = PKG_DIR / "phase2_backfill_draft.json"
FINAL_PATH = PKG_DIR / "phase2_backfill_final.json"

KIND = "verdict_backfill"
ERA = "v2-wc2026-backfill"

# Gate-verdict vocabulary the task/CLAUDE.md protocol actually uses in the
# narrative ("判定:Pass" / "判定:Watch" / initial "Bet候選" / an actually-
# placed "paper-bet"). Deliberately a closed map: an unrecognized draft
# `decision` value must be classified here on purpose, not silently passed
# through.
_GATE_MAP = {"PASS": "Pass", "WATCH": "Watch", "BET_CANDIDATE": "Bet候選",
             "BET": "paper-bet", None: None}

# Self-reported / registered contamination events that cannot be derived
# from lock/odds timestamps (see core/states.py TAINT_EVENTS + docstring
# above). Keyed by CANONICAL match_id.
HAND_CODED_CONTAMINATION = {
    "m97_fra_mar": ["leak_to_analyst"],
    "m92_mex_eng": ["missing"],
}

_NUM_SHORTHAND_RE = re.compile(r"^m(\d+)$")


def _gate_verdict(decision):
    if decision not in _GATE_MAP:
        raise ValueError(
            f"unrecognized decision value {decision!r} -- classify it against "
            f"the Pass/Watch/Bet候選/paper-bet vocabulary in _GATE_MAP "
            f"deliberately, not by ad hoc pass-through")
    return _GATE_MAP[decision]


def _canonicalize(raw_id, fixtures):
    """Draft/final source match_id -> canonical store match_id, via the
    fixture registry (merge rule a). Never hardcodes an id->id table:
      1. already canonical (the id IS some fixture's match_id verbatim --
         covers r16_por_esp, and any already-full "m<NN>_home_away" id)
      2. numeric shorthand "m<NN>" (m89, m101, ...) -> registry.resolve by
         match number, which also transparently covers REGISTERED_MISSING
         slots (m92 -> m92_mex_eng) since build_fixtures() includes those
         as fixtures too
      3. pilot shorthand ("aus_egy") -> the one pilot fixture whose
         match_id ends with "_" + raw_id (r32_aus_egy, ...)
    """
    for f in fixtures:
        if f.get("match_id") == raw_id:
            return raw_id
    m = _NUM_SHORTHAND_RE.match(raw_id)
    if m:
        f = registry.resolve(int(m.group(1)), fixtures)
        if f and f.get("match_id"):
            return f["match_id"]
    for f in fixtures:
        mid = f.get("match_id") or ""
        if f.get("pilot") and mid.endswith("_" + raw_id):
            return mid
    raise ValueError(
        f"cannot canonicalize match_id {raw_id!r} against the fixture "
        f"registry -- is it locked/imported yet?")


def _load_raw_matches(draft_path=None, final_path=None):
    """Merge the draft's 17 matches (with m97-m100 settle-refreshed) with
    the final file's 4 fresh matches. Returns (raw_matches, final_doc)."""
    draft = json.loads((draft_path or DRAFT_PATH).read_text(encoding="utf-8"))
    final = json.loads((final_path or FINAL_PATH).read_text(encoding="utf-8"))
    settle_refresh = final["settle_refresh"]

    raws = []
    for m in draft["matches"]:
        row = dict(m)
        refresh = settle_refresh.get(row["match_id"])
        if refresh:
            row.update(refresh)
        raws.append(row)
    raws.extend(final["fresh_matches"])
    return raws, final


def _build_row(raw, final_doc, fixtures, ledger_by_match, results_by_match,
                scoring_by_match):
    raw_id = raw["match_id"]
    canonical = _canonicalize(raw_id, fixtures)
    fx = registry.resolve(canonical, fixtures)
    pilot = bool(raw.get("sample") == "pilot")
    if fx is not None and fx.get("sample") is not None \
            and fx["sample"] != raw.get("sample"):
        raise ValueError(
            f"{canonical}: draft sample={raw.get('sample')!r} disagrees with "
            f"the registry's sample={fx['sample']!r} -- adjudicate, don't "
            f"silently trust either side")

    # --- merge rule (b): mechanism_ledger.jsonl is canonical -------------
    ledger = ledger_by_match.get(canonical)
    draft_j_types = raw.get("j_deviation_types")
    if ledger:
        j_deviation_types = ledger.get("mechanism_types")
        j_net_d_eff_shift = ledger.get("d_eff_shift")
    else:
        j_deviation_types = None
        j_net_d_eff_shift = None

    narrative_bits = [b for b in (raw.get("notes"), raw.get("notes_addendum"))
                       if b]
    if draft_j_types and ledger:
        narrative_bits.append(
            "[draft j_deviation_types, superseded by mechanism_ledger.jsonl "
            f"per RESTRUCTURE_PLAN merge rule b]: {draft_j_types}")

    # --- mechanical contamination join ------------------------------------
    sc = scoring_by_match.get(canonical)
    contamination = list(HAND_CODED_CONTAMINATION.get(canonical, []))
    if sc:
        if sc.get("contaminated"):
            contamination.append("odds_before_lock")
        if sc.get("contaminated_j"):
            contamination.append("post_market_j")

    # --- mechanical settle_outcome / b1 join -------------------------------
    res = results_by_match.get(canonical)
    settle_outcome = {
        "score90": res.get("score90") if res else None,
        "advanced": res.get("advanced") if res else None,
        "settled": bool(raw.get("settled")),
        "summary": raw.get("outcome"),
    }
    b1 = None
    if sc and sc.get("status") == "scored":
        b1 = {
            "model_rps": sc.get("model_rps"),
            "j_rps": sc.get("j_rps"),
            "market_rps": sc.get("market_rps"),
            "model_better": sc.get("model_better"),
            "j_better": sc.get("j_better"),
            "outcome_1x2": sc.get("outcome"),
        }

    # --- mechanism_materialization: dict form wins, else the override ----
    mech = raw.get("mechanism_materialization")
    legacy = raw.get("mechanism_materialized")
    if mech is None and legacy:
        mech = final_doc["mechanism_materialization_override"].get(canonical)
        if mech is None:
            raise ValueError(
                f"{canonical}: draft mechanism_materialized={legacy!r} has no "
                f"mechanism_materialization_override entry in "
                f"phase2_backfill_final.json -- classify it deliberately, "
                f"don't drop it silently")

    tq = final_doc["two_question"].get(raw_id)
    key_lesson = raw.get("key_lesson")
    if key_lesson is None:
        key_lesson = final_doc.get("key_lesson_override", {}).get(raw_id)

    return {
        "match_id": canonical,
        "match_num": fx.get("match_num") if fx else None,
        "pilot": pilot,
        "sample": raw.get("sample"),
        "gate_verdict": _gate_verdict(raw.get("decision")),
        "two_question": ({"answer": tq.get("answer"), "gist": tq.get("gist")}
                          if tq else None),
        "j_deviation_types": j_deviation_types,
        "j_net_d_eff_shift": j_net_d_eff_shift,
        "contamination": contamination,
        "settle_outcome": settle_outcome,
        "b1": b1,
        "mechanism_materialization": mech,
        "process_vs_outcome": raw.get("process_vs_outcome"),
        "key_lesson": key_lesson,
        "narrative_note": "\n\n".join(narrative_bits) if narrative_bits else None,
        "source_ref": {
            "file": "pilot_verdicts.md" if pilot else "verdicts_main.md",
            "section": raw.get("source_anchor"),
        },
        "gap_m_pp": raw.get("gap_m_pp"),
        "gap_j_pp": raw.get("gap_j_pp"),
        "market_error_mechanism_named": raw.get("market_error_mechanism_named"),
        "paper_bets": raw.get("paper_bets"),
    }


def build_rows(draft_path=None, final_path=None):
    """Assemble the merged, canonicalized backfill rows. Does not touch the
    store. Runs import_v2.import_all() first so the mechanical joins
    (results/mechanisms/scoring) always see a current store."""
    import_v2.import_all()
    fixtures = registry.build_fixtures(era=import_v2.ERA)
    raws, final_doc = _load_raw_matches(draft_path, final_path)

    ledger_by_match = {rec["data"]["match_id"]: rec["data"]
                        for rec in io.read("mechanisms", era=import_v2.ERA)}
    results_by_match = {rec["data"]["match_id"]: rec["data"]
                         for rec in io.read("results", era=import_v2.ERA)}
    scoring_by_match = {r["match_id"]: r
                         for r in scoring.score_rows(era=import_v2.ERA)}

    rows = []
    seen = set()
    for raw in raws:
        row = _build_row(raw, final_doc, fixtures, ledger_by_match,
                          results_by_match, scoring_by_match)
        if row["match_id"] in seen:
            raise ValueError(
                f"duplicate canonical match_id produced by the backfill "
                f"source data: {row['match_id']}")
        seen.add(row["match_id"])
        rows.append(row)
    return rows


def import_backfill(draft_path=None, final_path=None):
    """Idempotent: rebuild_era() atomically replaces the whole
    (verdict_backfill, ERA) slice every run, so re-running never
    duplicates rows (import_v2.py's own idempotency pattern)."""
    rows = build_rows(draft_path, final_path)
    records = [{"data": row, "match_id": row["match_id"],
                "source_path": "football_ai/phase2_backfill_draft.json + "
                               "football_ai/phase2_backfill_final.json"}
               for row in rows]
    return io.rebuild_era(KIND, ERA, records)


def _selfcheck():
    """Planted-fault checks against a fake, throwaway v2 tree + fake draft/
    final JSON files -- never touches the real wc2026/ files or the real
    phase2_backfill_*.json. Returns a list of failure strings (empty =
    pass)."""
    import tempfile

    global DRAFT_PATH, FINAL_PATH
    old_draft_path, old_final_path = DRAFT_PATH, FINAL_PATH
    old_v2_dir, old_store_dir = import_v2.V2_DIR, io.STORE_DIR
    fails = []

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fake_v2 = td / "wc2026"
        (fake_v2 / "locks").mkdir(parents=True)
        (fake_v2 / "odds" / "raw").mkdir(parents=True)

        def lock_doc(match_id, pilot, locked_at):
            return {
                "locked_at": locked_at, "version": 1,
                "payload": {"match_id": match_id, "pilot": pilot,
                            "teams": {"home": "H", "away": "A"},
                            "kickoff_utc": "2026-01-03T00:00:00+00:00",
                            "probs_90": {"home": 0.5, "draw": 0.3, "away": 0.2}},
                "sha256": "x"}

        # m89 slot: clean, has a ledger entry -> tests merge rule (b) +
        # numeric-shorthand canonicalization + the mechanism override path.
        (fake_v2 / "locks" / "m89_fake_home_away.json").write_text(
            json.dumps(lock_doc("m89_fake_home_away", False,
                                 "2026-01-01T00:00:00+00:00"),
                       ensure_ascii=False), encoding="utf-8")
        # a pilot slot, deliberately CONTAMINATED (locked after first odds
        # fetch) -> tests pilot-shorthand canonicalization + mechanical
        # odds_before_lock detection.
        (fake_v2 / "locks" / "r32_fake_pilot.json").write_text(
            json.dumps(lock_doc("r32_fake_pilot", True,
                                 "2026-01-02T00:00:00+00:00"),
                       ensure_ascii=False), encoding="utf-8")
        # m101 slot: no ledger entry, no results entry -> tests the honest-
        # null path for j_deviation_types / settle_outcome / b1.
        (fake_v2 / "locks" / "m101_fakenew_team.json").write_text(
            json.dumps(lock_doc("m101_fakenew_team", False,
                                 "2026-01-01T00:00:00+00:00"),
                       ensure_ascii=False), encoding="utf-8")

        def odds_line(match_id, fetched_at, closing):
            return {"match_id": match_id, "book": "pinnacle", "market": "1x2",
                     "line": None, "outcomes": ["home", "draw", "away"],
                     "odds": [2.0, 3.0, 4.0], "source": "test",
                     "fetched_at": fetched_at, "closing": closing, "note": ""}

        (fake_v2 / "odds" / "m89_fake_home_away.jsonl").write_text(
            json.dumps(odds_line("m89_fake_home_away",
                                  "2026-01-01T06:00:00+00:00", True),
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (fake_v2 / "odds" / "r32_fake_pilot.jsonl").write_text(
            json.dumps(odds_line("r32_fake_pilot",
                                  "2026-01-01T00:00:00+00:00", True),
                       ensure_ascii=False) + "\n", encoding="utf-8")

        results_doc = {
            "m89_fake_home_away": {"score90": [1, 0], "advanced": "home",
                                    "notes": "test"},
            "r32_fake_pilot": {"score90": [0, 0], "advanced": "away",
                                "notes": "test"},
        }
        (fake_v2 / "results.json").write_text(
            json.dumps(results_doc, ensure_ascii=False), encoding="utf-8")

        mech_line = {"match_id": "m89_fake_home_away",
                     "mechanism_types": ["fake_mech"], "d_eff_shift": 7,
                     "description": "test", "source_lock":
                         "locks/m89_fake_home_away.json"}
        (fake_v2 / "mechanism_ledger.jsonl").write_text(
            json.dumps(mech_line, ensure_ascii=False) + "\n", encoding="utf-8")

        draft_doc = {"matches": [
            {"match_id": "m89", "sample": "main", "lock_status": "clean",
             "decision": "PASS", "gap_m_pp": 1.0, "gap_j_pp": 1.0,
             "market_error_mechanism_named": False,
             "j_deviation_types": ["fake_free_text"], "paper_bets": 0,
             "settled": True, "outcome": "home win",
             "process_vs_outcome": "x", "mechanism_materialized": "partial",
             "source_anchor": "## fake m89", "notes": "draft note"},
            {"match_id": "fake_pilot", "sample": "pilot",
             "lock_status": "m_only", "decision": "PASS", "gap_m_pp": 2.0,
             "gap_j_pp": None, "market_error_mechanism_named": False,
             "j_deviation_types": None, "paper_bets": 0, "settled": True,
             "outcome": "draw", "process_vs_outcome": "y",
             "mechanism_materialized": None, "source_anchor": "## fake pilot",
             "notes": "pilot note"},
        ]}
        final_doc = {
            "two_question": {
                "m89": {"answer": "unanswered", "gist": "g1"},
                "fake_pilot": {"answer": None, "gist": None},
                "m101": {"answer": "answered", "gist": "fresh gist"},
            },
            "settle_refresh": {},
            "fresh_matches": [
                {"match_id": "m101", "sample": "main", "lock_status": "clean",
                 "decision": "WATCH", "gap_m_pp": 3.0, "gap_j_pp": 3.0,
                 "market_error_mechanism_named": True, "j_deviation_types": [],
                 "paper_bets": 0, "settled": False, "outcome": None,
                 "process_vs_outcome": None, "mechanism_materialization": None,
                 "key_lesson": None, "narrative_note": None,
                 "source_anchor": "## fake m101"},
            ],
            "mechanism_materialization_override": {
                "m89_fake_home_away": {"fake_mech": "not"}},
        }
        draft_path = td / "draft.json"
        final_path = td / "final.json"
        draft_path.write_text(json.dumps(draft_doc, ensure_ascii=False),
                               encoding="utf-8")
        final_path.write_text(json.dumps(final_doc, ensure_ascii=False),
                               encoding="utf-8")

        try:
            import_v2.V2_DIR = fake_v2
            io.STORE_DIR = td / "store"
            DRAFT_PATH = draft_path
            FINAL_PATH = final_path

            n1 = import_backfill(draft_path, final_path)
            if n1 != 3:
                fails.append(f"expected 3 backfill rows, got {n1}")

            by_id = {r["data"]["match_id"]: r["data"]
                     for r in io.read(KIND, era=ERA)}
            if len(by_id) != 3:
                fails.append(f"expected 3 stored rows, got {len(by_id)}")

            m89 = by_id.get("m89_fake_home_away")
            if m89 is None:
                fails.append("m89 numeric-shorthand canonicalization failed")
            else:
                if m89["gate_verdict"] != "Pass":
                    fails.append(f"m89 gate_verdict wrong: {m89['gate_verdict']}")
                if m89["j_deviation_types"] != ["fake_mech"]:
                    fails.append(
                        f"m89 j_deviation_types should come from the ledger "
                        f"(merge rule b), got {m89['j_deviation_types']}")
                if m89["j_net_d_eff_shift"] != 7:
                    fails.append("m89 j_net_d_eff_shift not joined from ledger")
                if not m89["narrative_note"] or "fake_free_text" not in m89["narrative_note"]:
                    fails.append(
                        "m89 narrative_note should carry the superseded draft "
                        "free-text j_deviation_types")
                if m89["mechanism_materialization"] != {"fake_mech": "not"}:
                    fails.append(
                        f"m89 mechanism_materialization override not applied: "
                        f"{m89['mechanism_materialization']}")
                if m89["contamination"] != []:
                    fails.append(f"m89 should be clean: {m89['contamination']}")
                want_settle = {"score90": [1, 0], "advanced": "home",
                                "settled": True, "summary": "home win"}
                if m89["settle_outcome"] != want_settle:
                    fails.append(
                        f"m89 settle_outcome mismatch: {m89['settle_outcome']} "
                        f"!= {want_settle}")
                if m89["two_question"] != {"answer": "unanswered", "gist": "g1"}:
                    fails.append("m89 two_question not applied")
                if m89["b1"] is None or m89["b1"]["model_better"] is not True:
                    fails.append(f"m89 b1 join wrong: {m89['b1']}")

            pilot_row = by_id.get("r32_fake_pilot")
            if pilot_row is None:
                fails.append("pilot-shorthand canonicalization failed")
            else:
                if pilot_row["pilot"] is not True:
                    fails.append("r32_fake_pilot not flagged pilot=True")
                if pilot_row["contamination"] != ["odds_before_lock"]:
                    fails.append(
                        f"pilot contamination should be mechanically detected: "
                        f"{pilot_row['contamination']}")

            m101 = by_id.get("m101_fakenew_team")
            if m101 is None:
                fails.append("m101 fresh-match canonicalization failed")
            else:
                if m101["gate_verdict"] != "Watch":
                    fails.append(f"m101 gate_verdict wrong: {m101['gate_verdict']}")
                if m101["j_deviation_types"] is not None:
                    fails.append(
                        "m101 has no ledger entry -- j_deviation_types must "
                        f"be null, not invented: {m101['j_deviation_types']}")
                if m101["settle_outcome"]["score90"] is not None:
                    fails.append("m101 has no results entry -- score90 must be null")
                if m101["b1"] is not None:
                    fails.append("m101 has no odds/results -- b1 must be null")
                if m101["two_question"] != {"answer": "answered", "gist": "fresh gist"}:
                    fails.append("m101 two_question not applied")

            # idempotence
            n2 = import_backfill(draft_path, final_path)
            if n2 != n1:
                fails.append(f"re-import changed row count: {n1} -> {n2}")
            if len(io.read(KIND, era=ERA)) != 3:
                fails.append("double-import duplicated backfill rows")

            # --- planted faults ------------------------------------------
            fixtures = registry.build_fixtures(era=import_v2.ERA)
            ledger_by_match = {r["data"]["match_id"]: r["data"]
                                for r in io.read("mechanisms", era=import_v2.ERA)}
            results_by_match = {r["data"]["match_id"]: r["data"]
                                 for r in io.read("results", era=import_v2.ERA)}
            scoring_by_match = {r["match_id"]: r
                                 for r in scoring.score_rows(era=import_v2.ERA)}

            try:
                _canonicalize("totally_unknown_id_xyz", fixtures)
                fails.append("unresolvable match_id must raise, not pass silently")
            except ValueError:
                pass

            try:
                _gate_verdict("MAYBE")
                fails.append("unrecognized decision value must raise")
            except ValueError:
                pass

            try:
                # "fake_pilot" canonicalizes fine (r32_fake_pilot) but has no
                # mechanism_materialization_override entry -- must raise
                # rather than silently drop the legacy fact.
                bad_raw = {"match_id": "fake_pilot", "sample": "pilot",
                           "decision": "PASS",
                           "mechanism_materialized": "no"}
                _build_row(bad_raw, final_doc, fixtures, ledger_by_match,
                           results_by_match, scoring_by_match)
                fails.append(
                    "a legacy mechanism_materialized value with no override "
                    "entry must raise, not silently drop the fact")
            except ValueError:
                pass

            try:
                dup_draft = dict(draft_doc)
                dup_draft["matches"] = draft_doc["matches"] + [draft_doc["matches"][0]]
                dup_path = td / "draft_dup.json"
                dup_path.write_text(json.dumps(dup_draft, ensure_ascii=False),
                                     encoding="utf-8")
                build_rows(dup_path, final_path)
                fails.append("a duplicate canonical match_id must raise")
            except ValueError:
                pass

        finally:
            import_v2.V2_DIR = old_v2_dir
            io.STORE_DIR = old_store_dir
            DRAFT_PATH = old_draft_path
            FINAL_PATH = old_final_path

    return fails


if __name__ == "__main__":
    n = import_backfill()
    print(f"verdict_backfill: {n} rows (era={ERA})")
    sys.exit(0)
