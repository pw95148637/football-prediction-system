"""The fixture registry: canonical match ids, aliases, main-sample membership.

`data/import_v2.py` moves the v2 files into the store byte-for-byte; it
does not interpret them. Interpretation — "which of these lock files is
match 96 of the main sample, which is a pilot, which main-sample slot has
no lock yet at all" — lives here, one level up, because it needs facts that
cannot be recovered from a lock file alone:

- MAIN_SAMPLE_NUMS: the pre-registered range of the falsification
  experiment (m89-m104). This is a PROTOCOL decision, not something a lock
  payload states about itself.
- ALIASES: a few matches were locked under a descriptive id instead of the
  "m<NN>_home_away" convention (r16_por_esp is really match 93 — the round
  name was used because "Portugal vs Spain" was the headline fixture of
  that round). The alias is a registered fact, not something regex-parsable
  from the id.
- REGISTERED_MISSING: m92 (Mexico-England) was never locked at all — an
  orphan-grab contamination incident documented in PROTOCOL's CHANGELOG
  (2026-07-06). It is a real slot in the main sample that permanently has
  no lock, which is different from a slot whose match just hasn't been
  played yet ("awaiting_lock"). Recording it here, rather than inferring
  it, is what lets build_fixtures tell those two states apart.

Everything else here (match_num parsing, fixture assembly, validation) is
mechanical and is read FROM the store — this module never reads locks/
files directly (that would duplicate import_v2's job and could drift from
what's actually in the store).

MULTI-COMPETITION NOTE (2026-07-24): `match_num`/`build_fixtures`/`validate`
take an optional `sample=None` (a `core.competition.SampleDefinition`).
`sample=None` means "use the WC2026 module constants above" -- every
existing caller is byte-behavior-identical with zero changes; this is the
one and only place those constants are consulted as a *default*, never
silently reused for someone else's competition. A passed-in `sample` with
`mode="fixed"` reproduces the same MAIN_SAMPLE_NUMS-style behavior with a
different population; `mode="dynamic"` (the normal round-robin-league
shape, research/audit/final_report.md section 4.1) drops the pre-registered
main/other split (there is no pre-registered sample to cherry-pick around)
and the awaiting_lock stub generation (a round-robin league doesn't
pre-declare which fixtures will exist the way a closed knockout bracket
does) -- see `_sample_of`/`build_fixtures`/`validate` below for exactly
what changes.
"""
import re
import sys
from collections import Counter

from football_ai.core.competition import SampleDefinition
from football_ai.store import io

ERA = "v2-wc2026"

# The falsification experiment's pre-registered main sample: R16 through
# the final, matches 89-104 inclusive.
MAIN_SAMPLE_NUMS = range(89, 105)

# id -> match number, for locks filed under a non-"m<NN>_..." id.
ALIASES = {"r16_por_esp": 93}

# Main-sample slots that will NEVER have a lock (contamination, not just
# "not played yet"). match_id here is the id results.json / PROTOCOL use
# for the slot even though no lock file exists for it.
REGISTERED_MISSING = {
    92: {
        "match_id": "m92_mex_eng",
        "reason": "orphan-grab contamination, never locked "
                  "(PROTOCOL CHANGELOG 2026-07-06)",
    },
}

_MATCH_ID_RE = re.compile(r"^m(\d+)_")


def match_num(match_id, payload, sample=None):
    """Match number for a lock, or None if it isn't a numbered main/other
    fixture (pilots have no match number by design — they are explicitly
    outside the falsification sample, not merely unnumbered).

    `sample`: an optional `core.competition.SampleDefinition`; None (the
    default) uses the WC2026 module constants above, byte-identical to
    this function's pre-2026-07-24 behavior."""
    if payload.get("pilot"):
        return None
    if sample is not None:
        if match_id in sample.aliases:
            return sample.aliases[match_id]
        return sample.parse_num(match_id)
    if match_id in ALIASES:
        return ALIASES[match_id]
    m = _MATCH_ID_RE.match(match_id or "")
    if m:
        return int(m.group(1))
    return None


def _sample_of(match_num_, pilot, sample=None):
    """`sample=None` (WC2026 default): "pilot" / "main" / "other", exactly
    as before. A provided `sample` with `mode="fixed"` reproduces that same
    membership-test shape against `sample.match_ids`. `mode="dynamic"` has
    no pre-registered population to test membership against at all -- every
    non-pilot fixture is simply "tracked"."""
    if pilot:
        return "pilot"
    if sample is not None:
        if sample.mode == "dynamic":
            return "tracked"
        return "main" if match_num_ in sample.match_ids else "other"
    if match_num_ in MAIN_SAMPLE_NUMS:
        return "main"
    return "other"


def build_fixtures(era, sample=None):
    """One fixture per match, assembled from the store's 'locks' records
    (era-filtered), plus registered facts that cannot come from a lock
    (known-missing slots) and, for a fixed/pre-registered population only,
    stub placeholders for slots that have neither a lock nor a
    known-missing entry yet.

    `era` IS REQUIRED (2026-08-28). It used to default to the module's
    `ERA = "v2-wc2026"`, which the generalization audit flagged as a silent
    generalization trap rather than a convenience: any caller that simply
    forgot the argument scoped itself to the archived World Cup window
    forever, with no error, warning or empty result to notice it by. Every
    real caller already passed it explicitly; only this module's own
    selfcheck and CLI were relying on the default, so making it required
    costs nothing and removes the whole class of silent mis-scoping for the
    competitions still to come.

    Sorted by match_num (fixtures with no match_num — i.e. pilots — sort
    last).

    `sample=None` (default): WC2026 behavior, unchanged -- REGISTERED_MISSING
    and MAIN_SAMPLE_NUMS drive the known-missing and awaiting_lock stubs
    exactly as before. A `mode="dynamic"` sample (the round-robin-league
    shape) produces neither -- there is no pre-registered population to
    stub gaps in, only whatever is actually locked."""
    all_records = io.read("locks", era=era)
    base_records = [r for r in all_records if r["data"].get("version") == 1]

    fixtures = []
    accounted_nums = set()

    for rec in base_records:
        doc = rec["data"]
        payload = doc.get("payload", {})
        mid = payload.get("match_id")
        siblings = [r for r in all_records
                    if r["data"].get("payload", {}).get("match_id") == mid]
        num = match_num(mid, payload, sample=sample)
        pilot = bool(payload.get("pilot"))
        has_j = any("j" in r["data"].get("payload", {}) for r in siblings)

        fixtures.append({
            "match_num": num,
            "match_id": mid,
            "teams": payload.get("teams"),
            "kickoff_utc": payload.get("kickoff_utc"),
            "pilot": pilot,
            "sample": _sample_of(num, pilot, sample=sample),
            "n_versions": len(siblings),
            "has_j": has_j,
            "status": "locked",
        })
        if num is not None:
            accounted_nums.add(num)

    known_missing = sample.known_missing if sample is not None else REGISTERED_MISSING
    for num, info in known_missing.items():
        fixtures.append({
            "match_num": num,
            "match_id": info["match_id"],
            "teams": None,
            "kickoff_utc": None,
            "pilot": None,
            "sample": _sample_of(num, False, sample=sample),
            "n_versions": 0,
            "has_j": False,
            "status": "missing",
        })
        accounted_nums.add(num)

    if sample is None:
        fixed_population = MAIN_SAMPLE_NUMS
    elif sample.mode == "fixed":
        fixed_population = sorted(sample.match_ids)
    else:
        fixed_population = None
    if fixed_population is not None:
        for num in fixed_population:
            if num not in accounted_nums:
                fixtures.append({"match_num": num, "status": "awaiting_lock"})

    fixtures.sort(key=lambda f: (f["match_num"] is None, f["match_num"]))
    return fixtures


def resolve(id_or_num, fixtures, sample=None):
    """Look up a fixture by canonical match_id, alias, or int match number.
    `sample=None` uses the WC2026 ALIASES constant, as before."""
    if isinstance(id_or_num, int):
        for f in fixtures:
            if f.get("match_num") == id_or_num:
                return f
        return None

    aliases = sample.aliases if sample is not None else ALIASES
    if id_or_num in aliases:
        num = aliases[id_or_num]
        for f in fixtures:
            if f.get("match_num") == num:
                return f

    for f in fixtures:
        if f.get("match_id") == id_or_num:
            return f
    return None


def validate(fixtures, sample=None):
    """Consistency errors as human-readable strings (empty = clean).

    Two checks, both defense-in-depth against the same failure mode (a
    lock's match_id silently colliding with, or falling outside, the
    registered population):
      - duplicates among locked tracked-sample fixtures. For a FIXED
        population (WC2026-shaped), match_num genuinely is a unique key
        (one knockout tie = one number), so duplicates are checked by
        match_num, as before. For a DYNAMIC population (a round-robin
        league), match_num is typically something like "gameweek" -- many
        matches legitimately share one gameweek, so checking match_num
        there would flag ~10 correct fixtures a week as "duplicates".
        Duplicates are checked by match_id instead: a real data-quality
        problem (the same lock filed twice) still gets caught, a normal
        multi-match round does not.
      - for a FIXED population only: a non-pilot lock whose match_id parses
        to a number outside it (checked independently of the 'sample'
        label build_fixtures already assigned, so a bug in that derivation
        can't hide the problem from validate too). A dynamic-mode
        competition has no bounded population to be "outside" of, so this
        second check is skipped for it, not vacuously satisfied.
    """
    errors = []
    locked = [f for f in fixtures if f.get("status") == "locked"]
    dynamic = sample is not None and sample.mode == "dynamic"
    tracked_label = "tracked" if dynamic else "main"
    tracked = [f for f in locked if f.get("sample") == tracked_label]

    if dynamic:
        ids = [f["match_id"] for f in tracked if f.get("match_id") is not None]
        for mid, count in sorted(Counter(ids).items()):
            if count > 1:
                errors.append(
                    f"duplicate match_id {mid!r} among {count} locked tracked-sample fixtures")
    else:
        nums = [f["match_num"] for f in tracked if f.get("match_num") is not None]
        for num, count in sorted(Counter(nums).items()):
            if count > 1:
                errors.append(
                    f"duplicate match_num {num} among {count} locked {tracked_label}-sample fixtures")

    if sample is None:
        population, lo, hi = MAIN_SAMPLE_NUMS, MAIN_SAMPLE_NUMS.start, MAIN_SAMPLE_NUMS.stop - 1
    elif sample.mode == "fixed":
        population = sample.match_ids
        lo, hi = (min(population), max(population)) if population else (None, None)
    else:
        population = None

    if population is not None:
        for f in locked:
            if f.get("pilot"):
                continue
            num = f.get("match_num")
            if num is not None and num not in population:
                errors.append(
                    f"non-pilot lock {f.get('match_id')} parses to match_num {num}, "
                    f"outside the registered population ({lo}-{hi})")

    return errors


def _print_table(fixtures):
    print(f"{'num':>4}  {'id':<20} {'status':<14} {'pilot':<6} has_j")
    for f in fixtures:
        num = f.get("match_num")
        num_s = "-" if num is None else str(num)
        print(f"{num_s:>4}  {f.get('match_id', '-'):<20} "
              f"{f.get('status', '-'):<14} {str(f.get('pilot', '-')):<6} "
              f"{f.get('has_j', '-')}")


def _selfcheck():
    """Planted-fault checks against a temp store — never touches the real
    football_ai/store/data/*.jsonl files. Returns a list of failure strings
    (empty = pass)."""
    import tempfile
    from pathlib import Path

    old_store_dir = io.STORE_DIR
    fails = []

    def _lock_record(match_id, version, pilot, source_path, extra_payload=None):
        payload = {
            "match_id": match_id,
            "pilot": pilot,
            "teams": {"home": "Home", "away": "Away"},
            "kickoff_utc": "2026-01-01T00:00:00+00:00",
        }
        if extra_payload:
            payload.update(extra_payload)
        doc = {"locked_at": "2026-01-01T00:00:00+00:00", "version": version,
               "payload": payload, "sha256": "x"}
        return {"data": doc, "source_path": source_path,
                "match_id": match_id, "version": version}

    with tempfile.TemporaryDirectory() as td:
        io.STORE_DIR = Path(td)
        try:
            records = [
                # a pilot lock (no match number, excluded from the main sample)
                _lock_record("r32_test_pilot", 1, True, "locks/r32_test_pilot.json"),
                # a plain main-sample lock: m95_x_y
                _lock_record("m95_x_y", 1, False, "locks/m95_x_y.json"),
                # the r16_por_esp alias for match 93
                _lock_record("r16_por_esp", 1, False, "locks/r16_por_esp.json"),
                # a lock (v1) + its v2 amend that adds j
                _lock_record("m96_test", 1, False, "locks/m96_test.json"),
                _lock_record("m96_test", 2, False, "locks/m96_test.v2.json",
                              extra_payload={"j": {"probs_90": {"home": 0.4}}}),
            ]
            io.rebuild_era("locks", ERA, records)

            fixtures = build_fixtures(era=ERA)

            pilot_fx = resolve("r32_test_pilot", fixtures)
            if pilot_fx is None or pilot_fx["sample"] != "pilot" or pilot_fx["match_num"] is not None:
                fails.append("pilot lock not classified as sample=pilot / match_num=None")

            main_fx = resolve("m95_x_y", fixtures)
            if main_fx is None or main_fx["sample"] != "main" or main_fx["match_num"] != 95:
                fails.append("m95_x_y not classified as sample=main / match_num=95")

            alias_fx_by_id = resolve("r16_por_esp", fixtures)
            alias_fx_by_num = resolve(93, fixtures)
            if alias_fx_by_id is None or alias_fx_by_id["match_num"] != 93:
                fails.append("r16_por_esp alias did not resolve to match_num 93")
            if alias_fx_by_num is None or alias_fx_by_num.get("match_id") != "r16_por_esp":
                fails.append("resolve(93, ...) did not find the r16_por_esp alias fixture")
            if alias_fx_by_id is not alias_fx_by_num:
                fails.append("resolve by alias id and by its num returned different fixtures")

            amend_fx = resolve("m96_test", fixtures)
            if amend_fx is None or amend_fx["n_versions"] != 2 or amend_fx["has_j"] is not True:
                fails.append(
                    f"m96_test amend not classified correctly: {amend_fx}")

            awaiting = resolve(101, fixtures)
            if awaiting is None or awaiting.get("status") != "awaiting_lock":
                fails.append("expected match_num 101 to be an awaiting_lock stub")

            missing = resolve(92, fixtures)
            if missing is None or missing.get("status") != "missing" \
                    or missing.get("match_id") != "m92_mex_eng":
                fails.append("m92 registered-missing stub not present/correct")

            base_errors = validate(fixtures)
            if base_errors:
                fails.append(f"validate() found spurious errors on clean fixtures: {base_errors}")

            # Plant a duplicate match_num among locked main fixtures and
            # confirm validate() catches it.
            dup_records = records + [
                _lock_record("m95_dup_dup", 1, False, "locks/m95_dup_dup.json"),
            ]
            io.rebuild_era("locks", ERA, dup_records)
            dup_fixtures = build_fixtures(era=ERA)
            dup_errors = validate(dup_fixtures)
            if not any("duplicate match_num 95" in e for e in dup_errors):
                fails.append(f"validate() did not catch planted duplicate match_num 95: {dup_errors}")

            # Plant a non-pilot lock whose parsed number falls outside the
            # main sample and confirm validate() catches that too.
            out_of_range_records = records + [
                _lock_record("m200_out_range", 1, False, "locks/m200_out_range.json"),
            ]
            io.rebuild_era("locks", ERA, out_of_range_records)
            oor_fixtures = build_fixtures(era=ERA)
            oor_errors = validate(oor_fixtures)
            if not any("match_num 200" in e for e in oor_errors):
                fails.append(f"validate() did not catch out-of-range match_num 200: {oor_errors}")

            # --- dynamic-mode (round-robin-league-shaped) sample ---
            # A completely different id convention (no "m<NN>_" prefix) and
            # no pre-registered population -- proves this isn't just the
            # WC2026 shape with a different range plugged in.
            dyn_sample = SampleDefinition(mode="dynamic",
                                           match_id_pattern=r"^gw(\d+)_")
            dyn_records = [
                _lock_record("gw03_ars_che", 1, False, "locks/gw03_ars_che.json"),
                _lock_record("gw03_liv_mci", 1, False, "locks/gw03_liv_mci.json"),
            ]
            io.rebuild_era("locks", "v1-dyn-test", dyn_records)
            dyn_fixtures = build_fixtures(era="v1-dyn-test", sample=dyn_sample)

            dyn_fx = resolve("gw03_ars_che", dyn_fixtures, sample=dyn_sample)
            if dyn_fx is None or dyn_fx["sample"] != "tracked" or dyn_fx["match_num"] != 3:
                fails.append(f"dynamic-mode fixture not classified as sample=tracked/match_num=3: {dyn_fx}")
            if any(f.get("status") == "awaiting_lock" for f in dyn_fixtures):
                fails.append("dynamic-mode build_fixtures must not generate awaiting_lock stubs (no fixed population)")
            if any(f.get("status") == "missing" for f in dyn_fixtures):
                fails.append("dynamic-mode build_fixtures must not generate missing stubs with an empty known_missing")

            dyn_errors = validate(dyn_fixtures, sample=dyn_sample)
            if dyn_errors:
                fails.append(f"validate() found spurious errors on clean dynamic-mode fixtures: {dyn_errors}")
            two_gw3 = [f for f in dyn_fixtures if f.get("match_num") == 3]
            if len(two_gw3) != 2:
                fails.append(
                    f"two distinct real matches sharing gameweek 3 must NOT be flagged as a "
                    f"match_num duplicate in dynamic mode (that would be true every single "
                    f"round of a real league) -- expected 2 clean gw=3 fixtures, got {len(two_gw3)}")

            # A genuinely duplicated match_id (the same lock filed twice, a
            # real data-quality bug) must still be caught in dynamic mode --
            # by match_id, not match_num, since match_num ("gameweek" here)
            # is legitimately many-to-one. A match_num (999) far outside
            # WC2026's 89-104 range must NOT be flagged for a dynamic
            # sample -- there is no bounded population to be "outside" of.
            dyn_dup_records = dyn_records + [
                _lock_record("gw03_ars_che", 1, False, "locks/gw03_ars_che_dupe.json"),
                _lock_record("gw999_x_y", 1, False, "locks/gw999_x_y.json"),
            ]
            io.rebuild_era("locks", "v1-dyn-test", dyn_dup_records)
            dyn_dup_fixtures = build_fixtures(era="v1-dyn-test", sample=dyn_sample)
            dyn_dup_errors = validate(dyn_dup_fixtures, sample=dyn_sample)
            if not any("duplicate match_id 'gw03_ars_che'" in e for e in dyn_dup_errors):
                fails.append(f"validate() did not catch planted duplicate match_id in dynamic mode: {dyn_dup_errors}")
            if any("999" in e for e in dyn_dup_errors):
                fails.append(f"validate() must not range-check match_num 999 for a dynamic (unbounded) sample: {dyn_dup_errors}")

        finally:
            io.STORE_DIR = old_store_dir

    try:
        build_fixtures()
        fails.append("build_fixtures() with no era must raise — a silent "
                     "default scoped every forgetful caller to the archived "
                     "WC2026 window forever")
    except TypeError:
        pass

    return fails


if __name__ == "__main__":
    fx = build_fixtures(era=ERA)
    _print_table(fx)
    errs = validate(fx)
    if errs:
        print("\nvalidation errors:")
        for e in errs:
            print(f"  - {e}")
    sys.exit(0)
