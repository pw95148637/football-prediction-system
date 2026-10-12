"""Mechanical leak validator for evidence-gathering subagent output.

WHY THIS FILE EXISTS AT THIS PATH (2026-08-26). This validator spent its
first six weeks living at `history/falsification-2026/firewall.py`, and on
2026-07-20 that whole directory became a frozen, read-only archive of a
closed experiment. But the validator never stopped being LIVE: every
`evidence/schema.py` bundle validation still reached into the archive by
sys.path to import it, so the single most load-bearing guardrail in the
system -- the one that decides whether a market number reaches the analyst
-- was infrastructure the project's own rules forbade anyone to touch. The
contradiction surfaced the first time it mattered: a live 2026-08-26 EPL
evidence run produced a suspected false positive, and the project's own
protocol for that case ("新誤攔類型先加測試再修" -- add the regression test
BEFORE fixing) had nowhere legal to write the test.

Resolution: the validator is promoted here, into the live package. The
archived copy is left BYTE-IDENTICAL and keeps its role -- it is what the
WC2026 sample was actually validated with, and `history/falsification-2026/
selftest.py` still tests it independently, so that experiment stays
reproducible from its own directory with zero edits. New rules land here.

THE DUPLICATION IS REAL, SO IT IS GATED. `_selfcheck` runs both
implementations over a shared corpus and requires identical verdicts,
except for entries explicitly listed in `DIVERGENCES` below. A silent drift
between the archived validator and the live one is therefore a test
failure, not a surprise six months later. This is the same discipline the
package already applies to the archived numbers (audit/selftest.py's
bit-identical v2 recompute gate).

THE RULE ITSELF IS UNCHANGED (2026-07-07 line, from the m97 failure where a
subagent wrote a prediction-market implied probability into an evidence
field and retired a whole arm): analytical figures -- xG, Elo, Opta/Dimers
modelled probabilities, expert stats -- are legitimate evidence and pass.
ONLY market-computed numbers are blocked: bookmaker odds, spread/total
lines, prediction-market implied probabilities, de-vigged market
probabilities. A hit means reject and have the scout redo it with the
number stripped. It is never a judgment about truth, only about whether a
number looks market-derived.
"""
import re

# Deliberate behavioural differences from the archived validator. Each entry
# is (case_id, text, live_expects_ok) -- `_selfcheck`'s equivalence gate
# skips these cases and asserts the NEW behaviour separately.
DIVERGENCES = [
    # False-positive class 5, found live 2026-08-26 on the first EPL evidence
    # bundle. A hyphenated magnitude RANGE ("84.5m-115m", a reported transfer
    # fee band) contains the token "-115", which is exactly the American
    # moneyline shape, and no Elo/rating word sits nearby to excuse it. The
    # archived validator flags it; this one does not, because a price is
    # never written with a magnitude suffix glued to it. Fixed narrowly (see
    # _MAGNITUDE_BEFORE_RE / _MAGNITUDE_AFTER_RE) rather than by loosening
    # the moneyline pattern itself: "-145 on the moneyline" must still be
    # caught, and the corpus above asserts that it is.
    ("fee range (FP class 5)",
     "sold Feltrin to Kingsbury United (reported 84.5m-115m, 4 May), Vance "
     "to Ashcombe (60m, 9 June) and Belka to Northvale (roughly 52m, 1 May)",
     True),
    # --- FALSE-NEGATIVE class 1, found 2026-10-06 by an adversarial probe from
    # a cold-start session. The archived validator is blind to a price that
    # is announced by the word "odds" (only "odds of" was a market word),
    # to the plurals "bookmakers"/"sportsbooks" (the \b boundary rejected
    # them), to "bookies", to a bare "handicap", to a price written in WORDS
    # ("at evens", "odds-on") and to fractional odds. A false negative
    # permanently retires a J, so these are blocked here and the archived
    # validator, which is what WC2026 was actually validated with, is left
    # exactly as it was.
    ("odds then a 1x2 price line",
     "Arsenal odds 1.40, draw 4.75, Leeds 7.50", False),
    ("plural bookmakers", "bookmakers make Arsenal 1.40 favourites", False),
    ("bookies", "Arsenal heavy favourites with the bookies at 1.4", False),
    ("bare handicap", "Arsenal -1.5 handicap at evens", False),
    ("fractional odds", "Arsenal are 2/5 to win", False),
    ("price in words: evens", "Arsenal are at evens with most firms", False),
    ("price in words: odds-on", "Leeds are odds-on to go down", False),
    # --- FALSE-NEGATIVE class 2, found 2026-10-07. Five GW06 scouts reported
    # prediction-market win probabilities and a sack-race price inside
    # WebSearch summaries. Probed against the phrasings they described, both
    # validators pass a market number that is attributed to "the prediction
    # market" or "the betting market" rather than to a named venue (the m97
    # case is caught only because "Kalshi" is in it), and a fractional price
    # attached to "favourite" with no "to win" after it. Modelled
    # probabilities (Opta, Dimers) remain analytical and pass -- the
    # 2026-07-07 line is unchanged; only the market attribution is new.
    ("prediction market, no venue named",
     "Ipswich were given a 25% chance by the prediction market", False),
    ("prediction markets, plural",
     "prediction markets give Fulham a 48% chance of winning", False),
    ("betting market", "the betting market has Fulham at 48%", False),
    ("favourite at a fractional price",
     "the sack-race market has Silva joint-favourite at 5/1", False),
    # widened the same day after an adversarial review of the first cut found
    # the same class still open in these shapes
    ("prediction-market, hyphenated",
     "prediction-market traders give Fulham 48%", False),
    ("markets give, no venue", "markets give Fulham a 48% chance", False),
    ("bookmaker name not yet listed", "Sky Bet have Fulham at 4/6", False),
    ("adjective before favourites", "Ipswich are 2/1 relegation favourites",
     False),
    ("hyphenated price and favourite", "Silva is 5-1 favourite", False),
    ("sack race price", "Silva is 5/1 in the sack race", False),
    ("labelled fractional 1x2 line", "Fulham 4/6, draw 12/5, Ipswich 4/1",
     False),
    ("punters", "punters backed Fulham into 4/6 overnight", False),
    ("price for the sack", "Silva is 5/1 for the sack", False),
]

# Words that mark a number as market-derived rather than analytical. Kept
# lowercase; matching is case-insensitive. Deliberately narrow (2026-07-07
# line) -- analytical sources (opta/xg/elo/fbref/...) are NOT in this list.
# NOTE: bare "spread" is deliberately absent -- team news legitimately says
# "illness spread through the camp"; only market-specific phrasings count.
#
# ACCEPTED FALSE POSITIVE, examined and deliberately NOT fixed (2026-09-03):
# "implied" fires on ordinary analytical prose -- "the 0.429 implied by the
# Bournemouth figure" was flagged on a real GW3 bundle. Unlike the fee-range
# case in DIVERGENCES, there is no structural discriminator here: nothing in
# the shape of "0.429 implied by X" distinguishes an analytical X from a
# market one, so narrowing it would take a semantic judgment the validator
# is not entitled to make, and "implied probability" is the exact phrase the
# m97 failure arrived in. The costs are asymmetric -- a false positive costs
# one rewrite, a false negative permanently invalidates a J arm -- so this
# stays. Scouts should write "derived from" / "corresponds to"; a claim
# flagged this way is rewritten, never waved through. The same holds for
# "betting market" next to an unrelated number ("public for 3 days, long
# enough for the betting market to react", 2026-10-07 review): the phrase is
# the market attribution itself, so the scout writes "for the news to be
# absorbed" instead.
MARKET_WORDS = [
    "pinnacle", "kalshi", "polymarket", "draftkings", "fanduel", "betmgm",
    "bet365", "sport888", "888sport", "1xbet", "betfair", "smarkets",
    "novig", "william hill", "williamhill", "betway", "bovada", "betonline",
    "unibet", "caesars", "betfred", "oddschecker", "oddsportal",
    "the-odds-api", "sportsbook", "sportsbooks", "bookmaker", "bookmakers",
    "bookie", "bookies", "book maker", "moneyline",
    "money line", "point spread", "spread line", "asian handicap",
    "handicap line", "handicap", "over/under", "totals line", "odds",
    "opened at",
    "priced at", "market price", "implied", "vig", "overround", "no-vig",
    "fair price", "closing line",
    # 2026-10-07, DIVERGENCES false-negative class 2: the venue is often not
    # named. Bare "market" stays out ("transfer market", "spent 120m in the
    # market") -- see _MARKET_VERB_RE for "markets give X 48%"; bare "bet"
    # and bare "betting" stay out ("a safe bet", "an 8-month betting ban",
    # "Silva is betting on a back three"), only the market phrasings count.
    "prediction market", "prediction markets", "prediction-market",
    "prediction-markets", "betting market", "betting markets", "betting odds",
    "betting exchange", "in the betting", "betting favourite",
    "betting favourites", "sack race", "sack-race", "punters", "bettors",
    "sky bet", "skybet", "paddy power", "paddypower", "ladbrokes", "coral",
    "betvictor", "betdaq", "predictit", "manifold",
    "隱含機率", "隱含賠率", "賠率", "盤口", "水位", "去水", "讓分盤",
    "大小盤", "devig", "de-vig", "市場賠率", "市場機率", "市場價",
]


def _word_pattern(w):
    """ASCII words get \\b boundaries ("vig" must not match Vigo/vigorous);
    CJK words match as substrings (continuous CJK text has no \\b breaks)."""
    p = re.escape(w)
    if w[0].isascii() and re.match(r"\w", w[0]):
        p = r"\b" + p
    if w[-1].isascii() and re.match(r"\w", w[-1]):
        p = p + r"\b"
    return p


# "the market(s) give/has/make ..." names a market as the source of a number
# without any venue or market word. "transfer market" is excluded by the
# lookbehind, which is the one place football prose says "market has".
_MARKET_VERB_RE = (r"(?<!transfer )\bmarkets?\s+(?:has|have|gives?|makes?|"
                   r"rates?|prices?)\b|\bper\s+(?:the\s+)?markets?\b")

_MARKET_WORDS_RE = re.compile(
    "|".join([_word_pattern(w) for w in MARKET_WORDS] + [_MARKET_VERB_RE]),
    re.IGNORECASE)

# A bare number or percentage, e.g. "62%", "-145", "2.10". The lookbehind
# skips digits glued to letters (sport888, wtop365, promo slugs) -- a price
# stands alone -- and digits mid-number (no re-matching "65" out of "365").
_NUM_RE = re.compile(r"(?<![A-Za-z\d])[+-]?\d+(?:\.\d+)?%?")

# URLs: digits inside a URL are slug/promo characters, never a readable
# price. Masked before number scanning.
_URL_RE = re.compile(r"https?://\S+")

# Date shapes are never a price: ISO dates ("2026-07-08", "2026/07/08"),
# bare years (1900-2099). Masked out before number detection so a book name
# next to an access date (the legitimate odds_exposure declaration format)
# does not trip the gate.
_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b")

# Markdown heading ordinals ("## 11. Odds exposure") are structural, not a
# price (found live 2026-07-16, m103_fra_eng_prelock.md).
_HEADING_ORDINAL_RE = re.compile(r"(?m)^#{1,6}[ \t]*\d+(?=\.)")

# American odds (moneyline) shape: +150 / -110 / -1450. Flagged even with no
# book word nearby -- EXCEPT when the nearby context is an analytical-rating
# phrase: Elo differences are written the same way ("Elo diff +404",
# "d_eff -146") and are core legitimate evidence, not a price.
_AMERICAN_ODDS_RE = re.compile(r"(?<![\d.])[+-]\d{3,5}(?![\d.%])")
_RATING_CONTEXT_RE = re.compile(
    r"elo|rating|d_eff|diff|delta|評分|分差", re.IGNORECASE)

# A magnitude suffix glued to the number on either side of the hyphen means
# this is a RANGE of amounts ("84.5m-115m", "60k-90k"), not a moneyline: no
# bookmaker writes a price as "-115m". Narrow by construction -- the suffix
# must be immediately adjacent, so "-145 minutes later" (a space) is still
# scanned as a possible price. See DIVERGENCES, FP class 5.
_MAGNITUDE_BEFORE_RE = re.compile(
    r"\d(?:\.\d+)?\s?(?:m|mn|bn|k|million|billion)$", re.IGNORECASE)
_MAGNITUDE_AFTER_RE = re.compile(
    r"^(?:m|mn|bn|k|million|billion)\b", re.IGNORECASE)

# Three decimal-odds-shaped numbers in a row (a 1x2 price triple), e.g.
# "2.10 3.30 3.80" or "2.10/3.30/3.80".
_ODDS_TRIPLE_RE = re.compile(
    r"\b([1-9]\d?\.\d{2})\s*[/,\-]?\s*([1-9]\d?\.\d{2})\s*[/,\-]?\s*"
    r"([1-9]\d?\.\d{2})\b")

# A price written in WORDS has no digit for the scan above to find: "at
# evens", "even money" and "odds-on" are each a price. Flagged on the phrase.
_PRICE_PHRASE_RE = re.compile(
    r"\b(?:at\s+evens|evens\s+favou?rites?|even\s+money|odds[- ]on)\b",
    re.IGNORECASE)

# Fractional odds: "2/5 to win", "11/4 to beat". The trailing verb is what
# separates a price from a statistic ("scored in 2/3 against Chelsea",
# "2/5 of the squad"), so it is required.
_FRACTIONAL_ODDS_RE = re.compile(
    r"\b\d{1,3}/\d{1,3}\s+to\s+(?:win|draw|beat|lose)\b", re.IGNORECASE)

# A fractional price attached to "favourite" ("joint-favourite at 5/1",
# "5/1 favourite", "2/1 relegation favourites", "5-1 favourite"), and a
# fractional price for the draw ("draw 12/5"). Adjacent only -- "favourites,
# winning 4/5 of their games" and "favourites in 7/10 home games" are
# statistics and must pass. DIVERGENCES false-negative class 2.
_FAVOURITE_FRACTION_RE = re.compile(
    r"\bfavou?rites?\s+at\s+\d{1,3}[/-]\d{1,3}\b"
    r"(?!\s+(?:of|home|away|games?|matches|league|times))"
    r"|\bfavou?rites?\s+\d{1,3}/\d{1,3}\b"
    r"|\b\d{1,3}[/-]\d{1,3}\s+(?:(?:joint|co|second|third|outright|"
    r"relegation|title|clear|hot|strong|heavy)[- ]?\s*)?favou?rites?\b"
    r"|\bdraw\s+(?:at\s+)?\d{1,3}/\d{1,3}\b"
    r"|\b\d{1,3}[/-]\d{1,3}\s+(?:to\s+be\s+)?(?:for|in|next\s+for)\s+the\s+sack\b",
    re.IGNORECASE)

PROXIMITY_CHARS = 60  # how close a number must sit to a market-word hit


def leaked_numbers(text):
    """Return the market-derived-looking (number, context) hits in `text`.

    Three patterns: an odds format (american-odds shape, or a 1x2-style
    triple of decimal-odds numbers), OR any number sitting within
    PROXIMITY_CHARS of a market-source keyword. A number with no market word
    nearby and no odds shape (an xG value, an Elo rating, a modelled
    probability, a transfer fee) is not flagged."""
    if not text:
        return []
    # context-display copy: dates masked (an access date is never a price)
    masked = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    # NUMBER-scanning copy. Order matters: URLs must be blanked on the
    # ORIGINAL text, before date masking can punch spaces into them and
    # break the \S+ run (found live 2026-07-09: a date inside a promo URL
    # split it, leaking "claim-150" past the URL mask). Then keyword spans
    # (brand digits: bet365/sport888/1xbet), then dates.
    numscan = list(text)

    def blank(mo):
        for i in range(mo.start(), mo.end()):
            numscan[i] = " "

    for um in _URL_RE.finditer(text):
        blank(um)
    for wm in _MARKET_WORDS_RE.finditer("".join(numscan)):
        blank(wm)
    for dm in _DATE_RE.finditer("".join(numscan)):
        blank(dm)
    for hm in _HEADING_ORDINAL_RE.finditer("".join(numscan)):
        blank(hm)
    numscan = "".join(numscan)
    hits = []
    for m in _AMERICAN_ODDS_RE.finditer(numscan):
        lo = max(0, m.start() - PROXIMITY_CHARS)
        hi = min(len(masked), m.end() + PROXIMITY_CHARS)
        window = masked[lo:hi]
        if _RATING_CONTEXT_RE.search(window):
            continue  # Elo-diff / rating-delta notation, not a moneyline
        if (_MAGNITUDE_BEFORE_RE.search(text[:m.start()])
                or _MAGNITUDE_AFTER_RE.match(text[m.end():])):
            continue  # hyphenated amount range (a fee band), not a price
        hits.append((m.group(0), window.strip(), "american_odds"))
    for m in _ODDS_TRIPLE_RE.finditer(numscan):
        lo = max(0, m.start() - PROXIMITY_CHARS)
        hi = min(len(masked), m.end() + PROXIMITY_CHARS)
        hits.append((m.group(0), masked[lo:hi].strip(), "odds_triple"))
    for m in (*_FRACTIONAL_ODDS_RE.finditer(numscan),
              *_FAVOURITE_FRACTION_RE.finditer(numscan)):
        lo = max(0, m.start() - PROXIMITY_CHARS)
        hi = min(len(masked), m.end() + PROXIMITY_CHARS)
        hits.append((m.group(0), masked[lo:hi].strip(), "fractional_odds"))
    # scanned on `masked`, not `numscan`: the market-word blanking above
    # erases "odds" inside "odds-on" before this pattern could see it
    for m in _PRICE_PHRASE_RE.finditer(masked):
        lo = max(0, m.start() - PROXIMITY_CHARS)
        hi = min(len(masked), m.end() + PROXIMITY_CHARS)
        hits.append((m.group(0), masked[lo:hi].strip(), "price_phrase"))
    for wm in _MARKET_WORDS_RE.finditer(masked):
        lo = max(0, wm.start() - PROXIMITY_CHARS)
        hi = min(len(masked), wm.end() + PROXIMITY_CHARS)
        for nm in _NUM_RE.finditer(numscan[lo:hi]):
            hits.append((nm.group(0), masked[lo:hi].strip(),
                         "number_near_market_word"))
    seen, out = set(), []
    for num, ctx, kind in hits:
        key = (num, ctx[:30])
        if key in seen:
            continue
        seen.add(key)
        out.append({"number": num, "context": ctx, "kind": kind})
    return out


def validate_bundle_text(text):
    """The leak validator gate. Returns (ok, leaks). ok=False means this
    subagent output must be rejected and redone -- never silently passed
    through to the analyst or the J runner."""
    leaks = leaked_numbers(text)
    return (len(leaks) == 0), leaks


def redact(leaks):
    """What may be shown to the ANALYST about a field's leaks: which kinds,
    how many -- never the number or the text around it. The analyst must not
    read flagged text (CLAUDE.md, price-blind reading); the scout that wrote the claim
    knows its own wording and only needs to be told where to look."""
    return {"n": len(leaks), "kinds": sorted({h.get("kind", "unknown")
                                              for h in leaks})}


def validate_bundle(bundle_fields):
    """Convenience wrapper for a structured evidence bundle (dict of str
    fields, e.g. {"claim": ..., "source": ..., "notes": ...}): validates
    every string field and returns {field: leaks} for any field that fails."""
    failures = {}
    for key, val in bundle_fields.items():
        if not isinstance(val, str):
            continue
        ok, leaks = validate_bundle_text(val)
        if not ok:
            failures[key] = leaks
    return failures


# The corpus both implementations must agree on. Every entry is a real
# lesson: the first four are the original 2026-07-07 rules, the rest are
# false positives that were caught in live use and turned into permanent
# tests rather than into "just bypass it this once".
EQUIVALENCE_CORPUS = [
    ("m97 leak", "Kalshi has France trading around 62% to advance, per the "
                 "prediction market.", False),
    ("analytical numbers", "Opta's supercomputer gives Spain 70.4% pregame; "
                           "Spain's xG this tournament is 2.84 per game, Elo "
                           "2159 vs Portugal 2013.", True),
    ("american odds", "USMNT opened at -145 on the moneyline before lineups "
                      "dropped.", False),
    ("book word, no number", "A market participant mentioned pricing but gave "
                             "no numbers — url-only exposure, no digits.", True),
    ("illness 'spread'", "Flu-like illness spread through the camp; 3 players "
                         "affected, James withdrawn at HT.", True),
    ("exposure declaration", "odds_exposure: URL-only, draftkings.com/soccer "
                             "slug seen in search results, accessed "
                             "2026-07-08, no numbers read.", True),
    ("elo-diff notation", "Elo diff +404 at neutral venue; d_eff -146.", True),
    ("vig inside Vigo", "Celta Vigo loanee played vigorously for 90 minutes, "
                        "2 chances created.", True),
    ("brand digits", "we cross-checked bet365 and sport888 preview pages but "
                     "copied no prices.", True),
    ("heading ordinal", "## 11. Odds exposure (which bookmaker pages were "
                        "opened)", True),
    ("odds triple", "the 1x2 line read 2.10 3.30 3.80 across the board", False),
    # --- club-competition evidence shapes (added 2026-08-26 with the EPL
    # window; a transfer fee is a fee, not a price) ---
    ("transfer fees", "sold Tonali on 6 July for GBP 92.5m, Guimaraes on 8 "
                      "August for GBP 75m, Gordon for GBP 69.5m", True),
    ("fee with add-ons", "a club-record deal worth up to GBP 100m (GBP 92.5m "
                         "up front plus GBP 7.5m in add-ons)", True),
    ("xg pair", "Newcastle recorded 1.39 xG from 13 shots against Liverpool's "
                "2.70 xG from 27 shots", True),
    ("squad numbers", "a 4-2-3-1 with 7 different players registering a shot "
                      "on target and 26 shots in total", True),
    # guards the narrowness of the FP-class-5 fix: a RANGE of actual prices
    # carries no magnitude suffix, so it must still be caught. Both
    # implementations agree here, which is the point.
    ("moneyline range", "the line moved from -145 to -160 overnight", False),
    # guards the other side of the 2026-10-06 tightening: prose that uses the
    # same words or shapes with no price in it must still pass
    ("favourites without a number",
     "Arsenal enter the match as title favourites after five straight league "
     "wins.", True),
    ("fractions that are statistics",
     "Leeds scored in 2/3 against top-six sides, and 3/5 of their goals came "
     "from set pieces.", True),
    ("handicapped is not handicap",
     "Leeds were handicapped by a short training week.", True),
    # guards the narrowness of false-negative class 2 (2026-10-07): a
    # modelled probability is analytical (the 2026-07-07 line), "market"
    # alone is the transfer market, and a fraction after "favourites," is a
    # statistic
    ("modelled probability (Dimers)",
     "Dimers' model gives Fulham 48% and Ipswich 25% after 10,000 "
     "simulations.", True),
    ("transfer market spend",
     "Fulham spent 120m in the summer transfer market across 6 signings.",
     True),
    ("favourites then a statistic",
     "Fulham enter as favourites, winning 4/5 of their home games.", True),
    ("betting ban", "Toney returns from an 8-month betting ban.", True),
    ("betting as a verb",
     "Silva is betting on the 3-4-3 that won 4 of the last 5.", True),
    ("FA betting rules",
     "a 10-month ban for breaching FA betting rules ended on 3 October.",
     True),
    ("favourites in n/m games",
     "Fulham were favourites in 7/10 home games last season.", True),
    ("a score is not a price",
     "Fulham beat the title favourites 2-1 in August.", True),
]


def _selfcheck():
    """Planted-fault checks + the anti-drift equivalence gate."""
    import sys
    from football_ai.core.paths import v2_archive_dir

    fails = []

    for case_id, text, should_pass in EQUIVALENCE_CORPUS:
        ok, leaks = validate_bundle_text(text)
        if ok != should_pass:
            verdict = "passed" if ok else f"flagged {[l['number'] for l in leaks]}"
            want = "pass" if should_pass else "flag"
            fails.append(f"corpus case {case_id!r}: expected {want}, {verdict}")

    # --- anti-drift gate: the live validator must agree with the frozen one
    # everywhere except where DIVERGENCES says otherwise. Without this, the
    # promotion out of the archive quietly forks the guardrail in two.
    archive = v2_archive_dir()
    if (archive / "firewall.py").exists():
        if str(archive) not in sys.path:
            sys.path.insert(0, str(archive))
        try:
            import firewall as archived
        except ImportError as e:  # pragma: no cover - archive missing
            fails.append(f"cannot import the archived validator to compare: {e}")
            archived = None
        if archived is not None:
            excused = {c[0] for c in DIVERGENCES}
            for case_id, text, _ in EQUIVALENCE_CORPUS:
                if case_id in excused:
                    continue
                mine = [h["number"] for h in leaked_numbers(text)]
                theirs = [h["number"] for h in archived.leaked_numbers(text)]
                if mine != theirs:
                    fails.append(
                        f"DRIFT on {case_id!r}: live={mine} archived={theirs} "
                        f"-- if intended, declare it in DIVERGENCES with a "
                        f"reason; if not, this is the bug")
            for case_id, text, expected_ok in DIVERGENCES:
                ok, _ = validate_bundle_text(text)
                if ok != expected_ok:
                    fails.append(f"declared divergence {case_id!r} does not "
                                 f"hold: expected ok={expected_ok}")
    else:
        fails.append("archived firewall.py not found — the equivalence gate "
                     "silently did nothing, which is worse than failing")

    # every hit says what KIND of price it looked like, because the analyst
    # is told the kind and the place -- never the number (see `redact`)
    kinds = {text: {h["kind"] for h in leaked_numbers(text)} for text in (
        "USMNT opened at -145 on the moneyline before lineups dropped.",
        "the 1x2 line read 2.10 3.30 3.80 across the board",
        "Arsenal are 2/5 to win", "Leeds are odds-on to go down",
        "Kalshi has France trading around 62% to advance")}
    for text, got in kinds.items():
        if not got or not got <= {"american_odds", "odds_triple",
                                  "fractional_odds", "price_phrase",
                                  "number_near_market_word"}:
            fails.append(f"hits must carry a known kind, got {got} for {text!r}")
    red = redact(leaked_numbers("Arsenal odds 1.40, draw 4.75, Leeds 7.50"))
    if red.get("n", 0) < 1 or any(num in repr(red)
                                  for num in ("1.40", "4.75", "7.50")):
        fails.append(f"redact() must say how many and what kind, and carry "
                     f"none of the flagged numbers, got {red!r}")

    # structured-bundle wrapper still reports per-field
    failures = validate_bundle({"claim": "Pinnacle opened it at 2.10",
                                "source": "BBC Sport", "n": 3})
    if "claim" not in failures or "source" in failures:
        fails.append(f"validate_bundle field routing wrong: {sorted(failures)}")

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
