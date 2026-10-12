"""Per-match decision-support report — the product surface (DECISION_PROTOCOL §3).

One command, deterministic, zero LLM calls:

    python -m football_ai.decision.report m100_arg_sui \
        [--mechanism "the market's specific error, if you can name one"]

Reads the v2 lock + odds files directly (the live authority until cutover)
and prints: instrument probabilities (M / J / Dixon-Coles distribution),
market read (devig + movement), an EV table at REAL snapshot prices, the
two-questions gate verdict, sizing advisory, and an honesty block with the
known instrument biases attached to every number that inherits them.

This is DECISION SUPPORT — it never feeds B1/B2, never rewrites locks, and
an EV figure here is "what this instrument implies at this real price",
not a recommendation. The gate line is the recommendation.

The deep edge-hunt workflow (~20 agents, ~1.5M tokens) is the paid upgrade
on top of this free report — run it only when this report leaves you with
a genuine open question (DECISION_PROTOCOL §5).

MULTI-COMPETITION NOTE (2026-07-24): `build()` takes an optional
`config=None` (a `core.competition.CompetitionConfig`). `config=None` is
byte-behavior-identical to this module's pre-2026-07-24 form: it reads the
WC2026 archive's raw V2_DIR files directly and known-bias list. A passed
`config` instead reads locks/odds/calibration through `store/io.py`
(era=`config.era`) -- the store already carries a mirrored view of the
WC2026 files too (`data/import_v2.py`), but re-pointing WC2026's own
default at the store is a behavior change this migration deliberately does
not make; only a new competition's calls opt into the store-based path.
`KNOWN_BIASES` is WC2026-sample-specific (m95/m96/m98/m100 diagnostics) --
a `config`-driven report starts with an empty bias list (no accumulated
track record yet for a brand-new competition window), not WC2026's.
"""
import argparse
import json
from pathlib import Path

from football_ai.core.paths import v2_archive_dir
from football_ai.market.consensus import consensus_fair, movement
from football_ai.market.devig import devig_shin
from football_ai.decision import sop
from football_ai.decision.sizing import ev as ev_of
from football_ai.models.m_dc import score_matrix
from football_ai.store import io as store_io

V2_DIR = v2_archive_dir()

KNOWN_BIASES = [
    "Elo/M overconfident on big favourites (pilot +6.5~24pp; m95 +12.8; "
    "m98 +10.9; m100 +5.6) — backing the favourite off M/J inherits this",
    "rest_travel mechanism: 0-for-1 on-pitch materialization in this "
    "project's own ledger (m96)",
    "DC rho fixes low-score cells only; it does NOT fix the favourite "
    "blowout tail (pred .788 vs emp .742, OOT) — margin/AH EVs stay "
    "structurally optimistic for favourites",
    "calibration-curve vs market totals tension, 6+ matches — diagnosed "
    "(not fully resolved): the Elo-diff-only lambda curve is blind to "
    "team-specific attack/defense identity that the market prices in; "
    "cross-validated via market-implied-lambda reconstruction + realized "
    "totals in m99 (curve lambdaSum 2.42 vs realized 1.72 combined over "
    "120', ~1.4/90'-normalized) and m100 (2.58 vs realized 2 goals in "
    "90') — totals EVs from this curve stay a known instrument blind "
    "spot, not free money",
]


# --- data access (v2 files are the live authority) ---------------------------

def load_lock(match_id):
    base = V2_DIR / "locks" / f"{match_id}.json"
    if not base.exists():
        raise FileNotFoundError(f"no lock at {base} — lock first (the "
                                f"report never runs pre-lock: reading odds "
                                f"for an unlocked match contaminates it)")
    versions = [json.loads(base.read_text(encoding="utf-8"))]
    for vf in sorted((V2_DIR / "locks").glob(f"{match_id}.v*.json")):
        versions.append(json.loads(vf.read_text(encoding="utf-8")))
    versions.sort(key=lambda d: d["version"])
    return versions


def load_snaps(match_id):
    p = V2_DIR / "odds" / f"{match_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in
            p.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_lock_from_store(match_id, era):
    """Store-based equivalent of load_lock(), for a competition whose data
    lives in store/io.py (era-tagged) rather than a raw V2_DIR file tree."""
    recs = [r for r in store_io.read("locks", era=era)
            if r.get("match_id") == match_id]
    if not recs:
        raise FileNotFoundError(
            f"no lock for {match_id!r} in the store (era={era!r}) — lock "
            f"first (the report never runs pre-lock: reading odds for an "
            f"unlocked match contaminates it)")
    versions = [r["data"] for r in recs]
    versions.sort(key=lambda d: d["version"])
    return versions


def load_snaps_from_store(match_id, era):
    """Store-based equivalent of load_snaps()."""
    return [r["data"] for r in store_io.read("odds", era=era)
            if r["data"].get("match_id") == match_id]


def dc_rho(era="v3"):
    """Full-window rho from the adopted m_dc gate record for `era`; None if
    absent. Matches by model name prefix "m_dc" so both the WC2026 module's
    own gate output ("m_dc") and a competition-specific calibration run
    (e.g. "m_dc (generalized replay_df)", football_ai/competitions/
    epl_2026_27.py) are found the same way."""
    recs = [r["data"] for r in store_io.read("calibrations", era=era)
            if str(r["data"].get("model", "")).startswith("m_dc")
            and r["data"].get("gate", {}).get("decision") == "adopted"]
    if not recs:
        return None
    return recs[-1]["full_window_params"]["rho"]


def _goal_curve(era=None):
    """(g_a, g_b), the market-free lambda curve M/DC use.

    era=None (default): read WC2026's V2_DIR/calibration.json directly --
    byte-identical to this function's pre-2026-07-24 behavior. era=<str>:
    read the same store-based "calibrations" record dc_rho() reads (model
    prefix "m_dc", full_window_params) -- for a competition with no raw
    V2-shaped calibration.json file, e.g. EPL's calibration is only ever
    persisted to the store (football_ai/competitions/epl_2026_27.py)."""
    if era is None:
        calib = json.loads((V2_DIR / "calibration.json")
                           .read_text(encoding="utf-8"))
        return calib["g_a"], calib["g_b"]
    recs = [r["data"] for r in store_io.read("calibrations", era=era)
            if str(r["data"].get("model", "")).startswith("m_dc")
            and r["data"].get("gate", {}).get("decision") == "adopted"]
    if not recs:
        raise ValueError(f"no adopted m_dc calibration found for era={era!r}")
    p = recs[-1]["full_window_params"]
    return p["g_a"], p["g_b"]


def lambdas_at(d_eff, era=None):
    import math
    g_a, g_b = _goal_curve(era=era)
    x = d_eff / 100.0
    return math.exp(g_a + g_b * x), math.exp(g_a - g_b * x)


def j_effective_d(payload):
    """J's implied effective Elo diff = M's d_eff + the signed sum of J's
    factor shifts. Only available when the lock carries decomposed factors
    (J_SPEC-era locks; m100 onward). This is what lets J's view propagate
    to margin/totals/score markets instead of living only in 1x2 — the
    'don't center everything on 1x2' fix at the decision layer. The
    registered B1 scoring is untouched (it stays 1x2 by registration)."""
    j = payload.get("j")
    if not j or not j.get("factors"):
        return None
    base = payload.get("inputs", {}).get("elo_diff_effective")
    if base is None:
        return None
    shift = sum(f["d_eff_shift"] * (1 if f.get("direction") == "home" else -1)
                for f in j["factors"])
    return base + shift


# --- DC grid helpers (quarter lines split into half-stakes, same convention
#     as wc2026/history/falsification-2026/model.py) ------------------------

def _grid_1x2(g):
    home = draw = away = 0.0
    n = g.shape[0]
    for i in range(n):
        for j in range(n):
            if i > j:
                home += g[i, j]
            elif i == j:
                draw += g[i, j]
            else:
                away += g[i, j]
    return home, draw, away


def _grid_totals(g, line):
    q = round(line * 4)
    if q % 2 == 1:
        o1, u1 = _grid_totals(g, (q - 1) / 4.0)
        o2, u2 = _grid_totals(g, (q + 1) / 4.0)
        return (o1 + o2) / 2.0, (u1 + u2) / 2.0
    over = under = push = 0.0
    n = g.shape[0]
    for i in range(n):
        for j in range(n):
            t = i + j
            if t > line + 1e-9:
                over += g[i, j]
            elif abs(t - line) <= 1e-9:
                push += g[i, j]
            else:
                under += g[i, j]
    live = over + under  # win-probability conditional on no push
    return (over / live, under / live) if live > 0 else (0.5, 0.5)


def _grid_ah_home(g, line):
    q = round(line * 4)
    if q % 2 == 1:
        w1, l1 = _grid_ah_home(g, (q - 1) / 4.0)
        w2, l2 = _grid_ah_home(g, (q + 1) / 4.0)
        return (w1 + w2) / 2.0, (l1 + l2) / 2.0
    win = lose = 0.0
    n = g.shape[0]
    for i in range(n):
        for j in range(n):
            adj = i + line - j
            if adj > 1e-9:
                win += g[i, j]
            elif adj < -1e-9:
                lose += g[i, j]
    live = win + lose
    return (win / live, lose / live) if live > 0 else (0.5, 0.5)


def _top_scores(g, k=5):
    n = g.shape[0]
    cells = [((i, j), g[i, j]) for i in range(n) for j in range(n)]
    cells.sort(key=lambda c: -c[1])
    return [(f"{i}-{j}", p) for (i, j), p in cells[:k]]


# --- report assembly ----------------------------------------------------------

def latest_and_open(snaps, market, line=None):
    def match(s):
        if s["market"] != market:
            return False
        if line is None and s.get("line") is None:
            return True
        if line is None or s.get("line") is None:
            return False
        return abs(s["line"] - line) < 1e-9
    cands = sorted((s for s in snaps if match(s)),
                   key=lambda s: s["fetched_at"])
    return (cands[-1], cands[0]) if cands else (None, None)


def per_book_latest(snaps, market, line=None, kickoff_utc=None):
    """Each book's most recent snapshot of one (market, line), pre-kickoff.

    A price is only real if you could still take it, so "best price" has to
    be assembled from each book's LATEST quote, never from the best number
    ever seen across the history -- that is the ghost-4.27 failure the
    charter names (`DECISION_PROTOCOL` section 5, "只有真實報價才算 EV")."""
    def match(s):
        if s.get("market") != market:
            return False
        if line is None and s.get("line") is None:
            return True
        if line is None or s.get("line") is None:
            return False
        return abs(s["line"] - line) < 1e-9

    out = {}
    for s in snaps:
        if not match(s):
            continue
        if kickoff_utc and s["fetched_at"] > kickoff_utc:
            continue
        cur = out.get(s["book"])
        if cur is None or s["fetched_at"] > cur["fetched_at"]:
            out[s["book"]] = s
    return out


def best_prices(book_snaps):
    """{outcome: {"price", "book", "at"}} -- the highest price on offer for
    each outcome across books, which is the price an EV calculation should
    use: nobody takes the median price when a better one is quoted."""
    best = {}
    for book, s in book_snaps.items():
        for outc, price in zip(s["outcomes"], s["odds"]):
            if outc not in best or price > best[outc]["price"]:
                best[outc] = {"price": price, "book": book,
                              "at": s["fetched_at"]}
    return best


def build(match_id, mechanism=None, config=None, abstention=None):
    """Assemble the decision report for one match.

    `config=None` (default): WC2026 behavior, unchanged -- reads V2_DIR raw
    files, era="v3" calibration lookup (currently empty -- WC2026's own
    m_dc gate has never actually been run; DC/rho stays dormant exactly as
    before), and the WC2026 KNOWN_BIASES list.

    `config` (a `core.competition.CompetitionConfig`): reads locks/odds
    through store/io.py and calibration through the store's "calibrations"
    kind, both filtered to `config.era`; starts with an empty known-bias
    list (see module docstring).

    `abstention`: the DECISION_PROTOCOL section 4 note for a PASS/WATCH --
    {"missing", "flips_if", "known_by"}. It is handed to `sop.evaluate`
    unchanged; without it every PASS/WATCH renders UNFINISHED, which is the
    intended default, not a bug (2026-09-03: until this parameter existed
    no caller COULD supply the note, so the UNFINISHED mark was
    unsatisfiable rather than a burden)."""
    if config is None:
        versions = load_lock(match_id)
        snaps = load_snaps(match_id)
        calib_era = "v3"
        goal_curve_era = None
        known_biases = KNOWN_BIASES
    else:
        versions = load_lock_from_store(match_id, config.era)
        snaps = load_snaps_from_store(match_id, config.era)
        calib_era = config.era
        goal_curve_era = config.era
        known_biases = list(getattr(config, "known_biases", ()) or ())
    gate_kwargs = ({} if config is None else
                   {"watch_pp": config.gate.watch_pp, "bet_pp": config.gate.bet_pp})
    final = versions[-1]["payload"]
    if config is not None and final.get("kickoff_utc"):
        # A price is only real if it could still be taken. The unattended job
        # keeps storing in-play quotes after kickoff, and the latest of those
        # (e.g. 7.56 / 1.27 / 7.17) is not the market's view of the match.
        snaps = [s for s in snaps if s["fetched_at"] <= final["kickoff_utc"]]
    m_probs = final["probs_90"]
    j = final.get("j")
    lam_h, lam_a = final["lambdas"]["home"], final["lambdas"]["away"]
    rho = dc_rho(era=calib_era)
    grid = score_matrix(lam_h, lam_a, rho) if rho is not None else None
    # J's distribution view: propagate J's factor shifts into the goal
    # model so margins/totals/scores carry J's information too, not just
    # the 1x2 vector (decision layer only; B1 stays 1x2 by registration)
    j_d = j_effective_d(final)
    grid_j = None
    if rho is not None and j_d is not None:
        jl_h, jl_a = lambdas_at(j_d, era=goal_curve_era)
        grid_j = score_matrix(jl_h, jl_a, rho)

    out = {"match_id": match_id,
           "teams": final.get("teams"),
           "kickoff_utc": final.get("kickoff_utc"),
           "instruments": {"M": m_probs,
                           "J": (j or {}).get("probs_90"),
                           "lambdas": [lam_h, lam_a], "dc_rho": rho,
                           "j_effective_d": j_d},
           "markets": [], "gate": None, "sizing": None,
           "honesty": {"known_biases": known_biases, "gaps": []}}
    # the amend guard writes this flag when a J was accepted after prices
    # existed. Until 2026-10-06 nothing read it back, so such a J rendered
    # exactly like a clean one.
    tainted = [v.get("version") for v in versions
               if v.get("contaminated_post_odds")]
    if tainted:
        out["honesty"]["gaps"].append(
            f"lock version(s) {tainted} were written AFTER market prices "
            f"existed (contaminated_post_odds): that J is not an independent "
            f"estimate -- weigh it accordingly and exclude it from any "
            f"scoring of instrument skill")
    if grid is not None:
        h, d, a = _grid_1x2(grid)
        out["instruments"]["DC_1x2"] = {"home": h, "draw": d, "away": a}
        out["instruments"]["DC_top_scores"] = _top_scores(grid)
    if grid_j is not None:
        h, d, a = _grid_1x2(grid_j)
        out["instruments"]["DCJ_1x2"] = {"home": h, "draw": d, "away": a}
        out["instruments"]["DCJ_top_scores"] = _top_scores(grid_j)

    gap_j_best = None
    # per-market blocks with EVs at REAL snapshot prices
    lines_seen = {(s["market"], s.get("line")) for s in snaps}
    for market, line in sorted(lines_seen, key=lambda x: (x[0], x[1] or 0)):
        if market == "qualify":
            continue  # manual-screenshot market; report if present below
        latest, opening = latest_and_open(snaps, market, line)
        if latest is None:
            continue

        # CROSS-BOOK READ (2026-08-31: a multi-book grab within quota had
        # become possible, so the report can read consensus instead of one
        # arbitrary book).
        #
        # The single-book form devigged ONE arbitrary book and then measured
        # both the EV and the instrument gap against that same book: nearly
        # circular, and with 25 books in the store the chosen one was
        # whichever happened to have the newest timestamp. Now the FAIR is
        # the cross-book consensus (median, robust to one stale book) and
        # the EV is measured against the BEST price actually on offer,
        # which is what a bettor would take. With a single book in the
        # store both reduce to exactly the old behaviour, so the WC2026
        # path is unchanged.
        book_snaps = per_book_latest(snaps, market, line,
                                     kickoff_utc=out.get("kickoff_utc"))
        cons = consensus_fair(snaps, market=market, line=line,
                              kickoff_utc=out.get("kickoff_utc"))
        best = best_prices(book_snaps) or dict(
            zip(latest["outcomes"],
                ({"price": p, "book": latest["book"],
                  "at": latest["fetched_at"]} for p in latest["odds"])))
        if cons.get("n_books", 0) > 1 and cons.get("consensus"):
            fair = cons["consensus"]
            fair_note = f"cross-book consensus of {cons['n_books']} books"
        else:
            fair = dict(zip(latest["outcomes"], devig_shin(latest["odds"])))
            fair_note = f"single book ({latest['book']})"

        blk = {"market": market, "line": line,
               "book": latest["book"], "at": latest["fetched_at"],
               "closing": latest.get("closing", False),
               "odds": {o: b["price"] for o, b in best.items()},
               "best_book": {o: b["book"] for o, b in best.items()},
               "n_books": cons.get("n_books", 1),
               "fair_note": fair_note,
               "market_fair": fair, "rows": []}
        if cons.get("n_books", 0) > 1:
            spread = {o: (max(f[o] for f in cons["books"].values() if o in f)
                          - min(f[o] for f in cons["books"].values() if o in f)) * 100
                      for o in fair}
            blk["book_dispersion_pp"] = spread
        # Movement is measured WITHIN one book. Built across books it is
        # not a price move at all -- with 25 books the "opening" snapshot was
        # simply whichever book sorted first by timestamp, so the delta
        # mixed a book change into a time change.
        mv = movement(snaps, latest["book"], market=market, line=line)
        if mv and mv["open_at"] != mv["latest_at"]:
            blk["movement_pp"] = mv["delta_pp"]
            blk["movement_book"] = latest["book"]
            blk["movement_span"] = (mv["open_at"][:16], mv["latest_at"][:16])
        if market == "1x2":
            inst = {"M": m_probs}
            if j:
                inst["J"] = j["probs_90"]
            if grid is not None:
                inst["DC"] = out["instruments"]["DC_1x2"]
            if grid_j is not None:
                inst["DCJ"] = out["instruments"]["DCJ_1x2"]
            for outc, price in blk["odds"].items():
                row = {"outcome": outc, "price": price}
                for tag, probs in inst.items():
                    row[f"ev_{tag}"] = ev_of(probs[outc], price)
                    row[f"gap_{tag}_pp"] = (probs[outc] - fair[outc]) * 100
                blk["rows"].append(row)
            if j:
                gap_j_best = max((r["gap_J_pp"] for r in blk["rows"]),
                                 default=None)
        elif market == "totals" and grid is not None:
            po, pu = _grid_totals(grid, line)
            pj = _grid_totals(grid_j, line) if grid_j is not None else None
            for k, (outc, p) in enumerate([("over", po), ("under", pu)]):
                price = blk["odds"].get(outc)
                if price:
                    row = {"outcome": outc, "price": price,
                           "ev_DC": ev_of(p, price),
                           "gap_DC_pp": (p - fair[outc]) * 100}
                    if pj is not None:
                        row["ev_DCJ"] = ev_of(pj[k], price)
                        row["gap_DCJ_pp"] = (pj[k] - fair[outc]) * 100
                    blk["rows"].append(row)
        elif market == "ah" and grid is not None:
            ph, pa = _grid_ah_home(grid, line)
            pj = _grid_ah_home(grid_j, line) if grid_j is not None else None
            for k, (outc, p) in enumerate([("home", ph), ("away", pa)]):
                price = blk["odds"].get(outc)
                if price:
                    row = {"outcome": outc, "price": price,
                           "ev_DC": ev_of(p, price),
                           "gap_DC_pp": (p - fair[outc]) * 100}
                    if pj is not None:
                        row["ev_DCJ"] = ev_of(pj[k], price)
                        row["gap_DCJ_pp"] = (pj[k] - fair[outc]) * 100
                    blk["rows"].append(row)
        # per-market advisory gate: largest absolute instrument gap in this
        # block, run through the same thresholds (gate_kwargs, computed
        # once from `config` above build()'s loop). ADVISORY for non-1x2 —
        # the registered thresholds are 1x2-native; the binding gate below
        # stays on J's 1x2 gap.
        gaps = [abs(v) for r in blk["rows"] for k, v in r.items()
                if k.startswith("gap_")]
        if gaps and market != "1x2":
            blk["advisory_gate"] = sop.evaluate(max(gaps), mechanism, None,
                                                abstention=abstention, **gate_kwargs)
            blk["advisory_gate"]["note"] = "advisory (1x2-native thresholds)"
        out["markets"].append(blk)

    # the two-questions gate on the J-vs-market 1x2 divergence
    if gap_j_best is not None:
        out["gate"] = sop.evaluate(gap_j_best, mechanism, None,
                                   abstention=abstention, **gate_kwargs)
        out["gate"]["gap_used_pp"] = gap_j_best
        out["gate"]["note"] = ("gate input = J's largest positive 1x2 gap; "
                               "BET_CANDIDATE additionally requires the "
                               "full anti-error checklist (not suppliable "
                               "from a CLI flag — deliberate friction)")
    else:
        out["honesty"]["gaps"].append("no J instrument or no 1x2 snapshot "
                                      "— gate not evaluated")
        # A fixture whose gate never ran still owes the DECISION_PROTOCOL section 4 note.
        # Until now UNFINISHED could only appear via a PASS/WATCH verdict,
        # so the fixtures with the WEAKEST instrument coverage -- exactly
        # the ones whose abstention most needs justifying -- were the only
        # ones that could never be marked unfinished. epl2627_gw03_ips_liv
        # (J foreclosed) rendered clean while owing everything.
        note, missing = sop._abstention_status(abstention)
        out["gate"] = {
            "decision": "NOT EVALUATED",
            "reasons": ["the gate could not run (no J instrument or no 1x2 "
                        "snapshot), which is not an answer -- charter section 4 "
                        "requires a committed lean on every fixture, and a "
                        "fixture the gate cannot reach owes it from the "
                        "judgment layer instead"],
            "failed_checks": [], "abstention": note,
        }
        if missing:
            out["gate"]["abstention_incomplete"] = list(missing)
    if out["gate"] and out["gate"]["decision"] == "BET_CANDIDATE":
        stake_u = config.gate.stake_u if config is not None else sop.STAKE_U
        max_bets = (config.gate.max_bets_per_match if config is not None
                    else sop.MAX_BETS_PER_MATCH)
        out["sizing"] = {"paper": f"fixed {stake_u}u (pre-registered; "
                                  f"confidence is not an input)",
                         "max_bets": max_bets}
    if not out["markets"]:
        out["honesty"]["gaps"].append("no odds snapshot at all yet — "
                                      "instrument probabilities only, no EV table")
    else:
        if not any(b.get("closing") for b in out["markets"]):
            out["honesty"]["gaps"].append("no closing snapshot yet — EVs are "
                                          "vs current prices, not the close")
        books = {b["book"] for b in out["markets"]}
        if len(books) <= 1:
            out["honesty"]["gaps"].append(f"single-book read ({next(iter(books))} only) — "
                                          "no cross-book consensus available")
    return out


def render(r):
    t = r.get("teams") or {}
    lines = [f"=== decision report {r['match_id']} "
             f"({t.get('home','?')} vs {t.get('away','?')}, "
             f"ko {r.get('kickoff_utc','?')}) ===",
             "DECISION SUPPORT ONLY — never feeds B1/B2 (DECISION_PROTOCOL)"]
    ins = r["instruments"]
    def fmt(p):
        return (f"{p['home']:.3f}/{p['draw']:.3f}/{p['away']:.3f}"
                if p else "—")
    rho_txt = (f"{ins['dc_rho']:.4f}" if ins.get("dc_rho") is not None
               else "n/a")
    lines.append(f"M  {fmt(ins['M'])}   J  {fmt(ins.get('J'))}   "
                 f"DC(M) {fmt(ins.get('DC_1x2'))}   "
                 f"DC(J) {fmt(ins.get('DCJ_1x2'))}")
    jd = ins.get("j_effective_d")
    lines.append(f"(lambdas {ins['lambdas'][0]:.2f}/{ins['lambdas'][1]:.2f}, "
                 f"rho {rho_txt}"
                 + (f", J effective d={jd:+.0f} -> J's view propagated to "
                    f"margins/totals/scores below" if jd is not None else "")
                 + ")")
    if ins.get("DC_top_scores"):
        lines.append("DC(M) top scores: " + ", ".join(
            f"{s} {p:.1%}" for s, p in ins["DC_top_scores"]))
    if ins.get("DCJ_top_scores"):
        lines.append("DC(J) top scores: " + ", ".join(
            f"{s} {p:.1%}" for s, p in ins["DCJ_top_scores"]))
    for blk in r["markets"]:
        line_txt = "" if blk["line"] is None else f"@{blk['line']}"
        closing = " CLOSING" if blk["closing"] else ""
        nb = blk.get("n_books", 1)
        head = f"{nb} books" if nb > 1 else blk["book"]
        lines.append(f"\n[{blk['market']}{line_txt}] {head}"
                     f"{closing} {blk['at'][:16]}")
        disp = blk.get("book_dispersion_pp")
        spread = f", spread {max(disp.values()):.1f}pp" if disp else ""
        lines.append("  fair: " + "  ".join(
            f"{k} {v:.3f}" for k, v in blk["market_fair"].items())
            + f"   ({blk.get('fair_note', '')}{spread})")
        if "movement_pp" in blk:
            span = blk.get("movement_span")
            when = f" [{span[0]} -> {span[1]}]" if span else ""
            lines.append(f"  move({blk.get('movement_book', '?')},pp){when}: "
                         + "  ".join(f"{k} {v:+.1f}"
                                     for k, v in blk["movement_pp"].items()))
        for row in blk["rows"]:
            evs = "  ".join(f"EV_{k[3:]} {v:+.1%}" for k, v in row.items()
                            if k.startswith("ev_"))
            bk = blk.get("best_book", {}).get(row["outcome"])
            at = f" ({bk})" if bk and nb > 1 else ""
            lines.append(f"    {row['outcome']:<6} @{row['price']:<6}{at:<15} {evs}")
        if "advisory_gate" in blk:
            ag = blk["advisory_gate"]
            lines.append(f"    advisory gate: {ag['decision']} "
                         f"({ag['note']})")
    if r["gate"]:
        g = r["gate"]
        gap = g.get("gap_used_pp")
        lines.append(f"\nGATE: {g['decision']}"
                     + (f"  (J gap {gap:+.1f}pp)" if gap is not None
                        else "  (no gap — the gate could not run)"))
        for reason in g["reasons"]:
            lines.append(f"  - {reason}")
        ab = g.get("abstention")
        if g.get("abstention_incomplete"):
            lines.append("  ** DECISION_PROTOCOL section 4: this abstention is "
                         "UNFINISHED — still owes "
                         + ", ".join(g["abstention_incomplete"]) + " **")
        elif ab:
            lines.append(f"  abstains because: {ab['missing']}")
            lines.append(f"  would flip if:    {ab['flips_if']}")
            lines.append(f"  knowable by:      {ab['known_by']}")
    if r["sizing"]:
        lines.append(f"SIZING: {r['sizing']}")
    lines.append("\nHONESTY:")
    for gap in r["honesty"]["gaps"]:
        lines.append(f"  data gap: {gap}")
    for b in r["honesty"]["known_biases"]:
        lines.append(f"  known bias: {b}")
    return "\n".join(lines)


def _selfcheck():
    import tempfile
    global V2_DIR
    fails = []

    # --- cross-book read (2026-08-31) -------------------------------------
    # The single-book form must stay BYTE-IDENTICAL to the pre-change
    # behaviour: the archived WC2026 reports are single-book and this module
    # is still their renderer.
    def _snap(book, odds, at, market="1x2", line=None):
        return {"book": book, "market": market, "line": line,
                "outcomes": ["home", "draw", "away"], "odds": odds,
                "fetched_at": at, "closing": False}

    one = [_snap("pinnacle", [2.30, 3.40, 3.20], "2026-01-01T00:00:00+00:00")]
    bp = best_prices(per_book_latest(one, "1x2"))
    if [bp[o]["price"] for o in ("home", "draw", "away")] != [2.30, 3.40, 3.20]:
        fails.append("single-book best prices must be that book's own prices")
    c1 = consensus_fair(one, market="1x2")
    solo = dict(zip(one[0]["outcomes"], devig_shin(one[0]["odds"])))
    if c1["n_books"] != 1:
        fails.append(f"n_books should be 1, got {c1['n_books']}")
    if any(abs(c1["consensus"][o] - solo[o]) > 1e-12 for o in solo):
        fails.append("one-book consensus must equal that book's own devig, or "
                     "the archived single-book reports would silently change")

    # a book's own superseded quote must not survive as a "best price"
    stale = [_snap("pinnacle", [2.10, 3.40, 3.60], "2026-01-01T00:00:00+00:00"),
             _snap("pinnacle", [2.30, 3.40, 3.20], "2026-01-02T00:00:00+00:00")]
    if best_prices(per_book_latest(stale, "1x2"))["away"]["price"] != 3.20:
        fails.append("a superseded quote must not be offered as a best price "
                     "— that is the ghost-price failure the charter names")

    three = [_snap("a", [2.30, 3.40, 3.20], "2026-01-02T00:00:00+00:00"),
             _snap("b", [2.45, 3.30, 3.05], "2026-01-02T00:00:00+00:00"),
             _snap("c", [2.25, 3.60, 3.10], "2026-01-02T00:00:00+00:00")]
    bp3 = best_prices(per_book_latest(three, "1x2"))
    got = (bp3["home"]["book"], bp3["draw"]["book"], bp3["away"]["book"])
    if got != ("b", "c", "a"):
        fails.append(f"best price is per OUTCOME across books, got {got}")
    c3 = consensus_fair(three, market="1x2")
    if c3["n_books"] != 3:
        fails.append(f"n_books should be 3, got {c3['n_books']}")
    if abs(sum(c3["consensus"].values()) - 1.0) > 1e-12:
        fails.append("consensus must renormalize to 1")

    late = three + [_snap("d", [9.9, 9.9, 9.9], "2026-01-05T00:00:00+00:00")]
    if "d" in per_book_latest(late, "1x2",
                              kickoff_utc="2026-01-03T00:00:00+00:00"):
        fails.append("a snapshot fetched after kickoff must be excluded")

    old_v2, old_store = V2_DIR, store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "locks").mkdir()
        (tdp / "odds").mkdir()
        (tdp / "calibration.json").write_text(
            json.dumps({"g_a": 0.1735, "g_b": 0.1961}), encoding="utf-8")
        store_io.STORE_DIR = tdp / "store"
        store_io.append("calibrations", {
            "model": "m_dc", "gate": {"decision": "adopted"},
            "full_window_params": {"rho": -0.036}}, era="v3")
        lock = {"locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
                "payload": {"match_id": "m_rep", "pilot": False,
                            "teams": {"home": "A", "away": "B"},
                            "kickoff_utc": "2026-01-03T00:00:00+00:00",
                            "probs_90": {"home": 0.5, "draw": 0.3,
                                         "away": 0.2},
                            "lambdas": {"home": 1.4, "away": 1.0},
                            "inputs": {"elo_diff_effective": 100},
                            "j": {"probs_90": {"home": 0.6, "draw": 0.25,
                                               "away": 0.15},
                                  "factors": [
                                      {"mechanism_type": "rest_travel",
                                       "direction": "home",
                                       "d_eff_shift": 50}]}},
                "sha256": "x"}
        (tdp / "locks" / "m_rep.json").write_text(json.dumps(lock),
                                                  encoding="utf-8")
        snap = {"match_id": "m_rep", "book": "pinnacle", "market": "1x2",
                "line": None, "outcomes": ["home", "draw", "away"],
                "odds": [2.0, 3.0, 6.0], "closing": False,
                "fetched_at": "2026-01-02T00:00:00+00:00"}
        (tdp / "odds" / "m_rep.jsonl").write_text(
            json.dumps(snap) + "\n", encoding="utf-8")
        V2_DIR = tdp
        try:
            r = build("m_rep")
            row_h = next(x for x in r["markets"][0]["rows"]
                         if x["outcome"] == "home")
            if abs(row_h["ev_M"] - (0.5 * 2.0 - 1.0)) > 1e-12:
                fails.append("EV math wrong (M home)")
            if abs(row_h["ev_J"] - (0.6 * 2.0 - 1.0)) > 1e-12:
                fails.append("EV math wrong (J home)")
            if r["gate"]["decision"] != "PASS":
                fails.append("no mechanism must gate to PASS however "
                             "large the gap")
            r2 = build("m_rep", mechanism="named mechanism")
            if r2["gate"]["decision"] not in ("WATCH", "BET_CANDIDATE"):
                fails.append("mechanism + big gap should not PASS")
            # DECISION_PROTOCOL section 4 wiring: the note must reach the gate
            # through build(), and its absence must render UNFINISHED.
            # (Until 2026-09-03 build() had no such parameter, so every
            # PASS/WATCH was UNFINISHED with no way to finish it.)
            if not r["gate"].get("abstention_incomplete"):
                fails.append("a PASS built without an abstention note must "
                             "be flagged incomplete")
            if "UNFINISHED" not in render(r):
                fails.append("an incomplete abstention must render UNFINISHED")
            # A fixture the gate CANNOT reach still owes the note. The
            # fixtures with the weakest instrument coverage used to be the
            # only ones that could never render UNFINISHED, which inverted
            # the requirement: least evidence, least accountability.
            lock_noj = json.loads(json.dumps(lock))
            lock_noj["payload"].pop("j")
            lock_noj["match_id"] = "m_noj"
            (tdp / "locks" / "m_noj.json").write_text(
                json.dumps(lock_noj), encoding="utf-8")
            (tdp / "odds" / "m_noj.jsonl").write_text(
                json.dumps(dict(snap, match_id="m_noj")) + "\n",
                encoding="utf-8")
            r_noj = build("m_noj")
            if r_noj["gate"]["decision"] != "NOT EVALUATED":
                fails.append(f"a fixture with no J must report the gate as "
                             f"NOT EVALUATED, got {r_noj['gate']['decision']!r}")
            if "UNFINISHED" not in render(r_noj):
                fails.append("a fixture the gate could not reach still owes "
                             "the DECISION_PROTOCOL section 4 note and must render UNFINISHED")
            r_noj2 = build("m_noj", abstention={
                "missing": "a clean J", "flips_if": "nothing before kickoff",
                "known_by": "already known"})
            if r_noj2["gate"].get("abstention_incomplete"):
                fails.append("a complete note must satisfy the ungated path too")

            note = {"missing": "priced-information mechanism",
                    "flips_if": "confirmed XI without both centre-backs",
                    "known_by": "T-75 lineup drop"}
            r3 = build("m_rep", abstention=note)
            if r3["gate"].get("abstention_incomplete"):
                fails.append("a complete abstention note passed to build() "
                             f"must not be flagged: {r3['gate']['abstention_incomplete']}")
            if "UNFINISHED" in render(r3) or "knowable by" not in render(r3):
                fails.append("a complete abstention note must render as "
                             "finished, with its three fields shown")
            if "DC_1x2" not in r["instruments"]:
                fails.append("DC instrument missing despite adopted rho")
            # J propagation: effective d = 100 + 50 (home-signed factor)
            if r["instruments"].get("j_effective_d") != 150:
                fails.append(f"j_effective_d wrong: "
                             f"{r['instruments'].get('j_effective_d')}")
            if "DCJ_1x2" not in r["instruments"]:
                fails.append("DC(J) view missing despite decomposed "
                             "factors — J stuck in 1x2 again")
            if not (r["instruments"]["DCJ_1x2"]["home"]
                    > r["instruments"]["DC_1x2"]["home"]):
                fails.append("DC(J) at higher d must favour home more "
                             "than DC(M)")
            render(r)  # must not raise
            try:
                build("m_missing")
                fails.append("missing lock must raise (pre-lock reads are "
                             "contamination)")
            except FileNotFoundError:
                pass
        finally:
            V2_DIR, store_io.STORE_DIR = old_v2, old_store

    # --- config-driven (store-based) path, e.g. a new competition with no
    # V2_DIR raw file tree at all -- proves build() actually reads through
    # store/io.py rather than just accepting the parameter and ignoring it.
    from football_ai.core.competition import (
        CompetitionConfig, DataSources, EloTierTable, GateThresholds,
        HomeAdvantageModel, SampleDefinition,
    )
    era = "test-report-dynamic"
    cfg = CompetitionConfig(
        competition_id="test-league", era=era, format="round_robin",
        sample=SampleDefinition(mode="dynamic", match_id_pattern=r"^t(\d+)_"),
        home_advantage=HomeAdvantageModel(mode="standard", home_adv_elo=60.0),
        elo_tiers=EloTierTable(default_k=20), mechanism_min_n=10,
        gate=GateThresholds(watch_pp=6.5, bet_pp=10.0, stake_u=2.0,
                             max_bets_per_match=1),
        data_sources=DataSources(rating_source="x", results_source="y",
                                  odds_book_default="betfair"))
    old_store = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            store_io.append("locks", {
                "locked_at": "2026-09-01T00:00:00+00:00", "version": 1,
                "payload": {"match_id": "t1_x_y", "pilot": False,
                            "teams": {"home": "X", "away": "Y"},
                            "kickoff_utc": "2026-09-03T00:00:00+00:00",
                            "probs_90": {"home": 0.4, "draw": 0.3, "away": 0.3},
                            "lambdas": {"home": 1.3, "away": 1.1},
                            # fair market home ~= 0.417 (devig_shin of the
                            # odds below); J at 0.49 -> ~7.3pp gap, deliberately
                            # between an EPL-shaped watch_pp=6.5/bet_pp=10 (-> WATCH)
                            # and WC2026's default watch_pp=3/bet_pp=5 (-> would be
                            # BET_CANDIDATE) -- the two configs must disagree here.
                            "j": {"probs_90": {"home": 0.49, "draw": 0.26,
                                               "away": 0.25}}},
                "sha256": "x"}, era=era, match_id="t1_x_y", version=1)
            store_io.append("odds", {
                "match_id": "t1_x_y", "book": "betfair", "market": "1x2",
                "line": None, "outcomes": ["home", "draw", "away"],
                "odds": [2.3, 3.3, 3.2], "closing": False,
                "fetched_at": "2026-09-02T00:00:00+00:00"}, era=era)
            store_io.append("odds", {
                "match_id": "t1_x_y", "book": "betfair", "market": "1x2",
                "line": None, "outcomes": ["home", "draw", "away"],
                "odds": [8.0, 1.3, 7.0], "closing": False,
                "fetched_at": "2026-09-03T01:00:00+00:00"}, era=era)  # in-play, after kickoff
            store_io.append("calibrations", {
                "model": "m_dc (generalized replay_df)",
                "gate": {"decision": "adopted"},
                "full_window_params": {"g_a": 0.27, "g_b": 0.17, "rho": -0.03}},
                era=era)

            r = build("t1_x_y", mechanism="a stated mechanism", config=cfg)
            if r["honesty"]["known_biases"] != []:
                fails.append("a config-driven report must start with an "
                             f"empty known-bias list, got {r['honesty']['known_biases']}")
            if r["instruments"].get("dc_rho") != -0.03:
                fails.append(
                    f"config-driven build() did not read rho from the store "
                    f"(era={era!r}); got {r['instruments'].get('dc_rho')}")
            if not any("betfair" in g for g in r["honesty"]["gaps"]):
                fails.append(
                    "single-book honesty message should name the actual "
                    f"book (betfair), not a hardcoded one: {r['honesty']['gaps']}")
            # ~7.3pp gap: below the EPL-shaped bet_pp=10 -> should land WATCH,
            # not BET_CANDIDATE -- proves config.gate actually drives the
            # decision (WC2026's own default bet_pp=5 would have made this
            # BET_CANDIDATE instead, sop.py's own selfcheck already covers
            # that default path).
            if r["gate"]["decision"] != "WATCH":
                fails.append(
                    f"config.gate thresholds were not honored -- expected WATCH "
                    f"for a ~7.3pp gap under bet_pp=10, got {r['gate']['decision']}")
            blk = next(m for m in r["markets"] if m["market"] == "1x2")
            if blk["at"] != "2026-09-02T00:00:00+00:00" or blk["market_fair"]["draw"] > 0.4:
                fails.append("a quote fetched after kickoff must not become the market "
                             f"read: at={blk['at']}, fair={blk['market_fair']}")
            try:
                build("nonexistent_id", config=cfg)
                fails.append("a config-driven build() on a never-locked id must raise")
            except FileNotFoundError:
                pass
        finally:
            store_io.STORE_DIR = old_store

    return fails


def main():
    ap = argparse.ArgumentParser(description="per-match decision report")
    ap.add_argument("match_id")
    ap.add_argument("--mechanism", default=None,
                    help="the market's specific error, if you can name one "
                         "(unnamed -> PASS however large the gap)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--missing", default=None,
                    help="DECISION_PROTOCOL section 4: which class of information "
                         "is missing (required for a finished PASS/WATCH)")
    ap.add_argument("--flips-if", default=None,
                    help="section 4: the observable event that would flip the verdict")
    ap.add_argument("--known-by", default=None,
                    help="section 4: when that event is knowable at the latest")
    args = ap.parse_args()
    abstention = None
    if args.missing or args.flips_if or args.known_by:
        abstention = {"missing": args.missing, "flips_if": args.flips_if,
                      "known_by": args.known_by}
    r = build(args.match_id, args.mechanism, abstention=abstention)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json
          else render(r))


if __name__ == "__main__":
    main()
