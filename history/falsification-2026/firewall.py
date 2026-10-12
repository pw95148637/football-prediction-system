"""Mechanical leak validator for evidence-gathering subagent output.

WC2026_Architecture_Review_v3.md section 4.4 / Migration Plan Phase 0(c):
the m97 failure (2026-07-07, France-Morocco retired to M-only) was a
subagent writing "Kalshi France 62%" into odds_exposure — a market-derived
NUMBER reaching the analyst before lock. PROTOCOL/SKILL.md already draws the
line in prose ("分析數據的數字照報是合法輸入；只有市場算好的數字禁入");
this module is the same rule enforced as a keyword/pattern check — not a
judgment about truth, only about whether a number looks market-derived.
(Pattern lineage: the red_check mechanical guard from the upstream
investment-OS project this package originated in; self-contained here.)

The line, unchanged (2026-07-07): analytical figures (xG, Elo, Opta/Dimers
modeled probabilities, expert stats) are legitimate J input and pass. ONLY
market-computed numbers (bookmaker odds, spread/total lines, prediction-market
implied probabilities, de-vigged market probabilities) are blocked.

Every evidence-gathering subagent's raw output should go through
`validate_bundle_text` before it reaches the analyst or a J runner. A hit
means: reject and have the subagent redo it with the leaked number stripped
(URL/slug-only references to a book are fine — see SKILL.md; only the
NUMBER is the hard block).
"""
import re

# Words that mark a number as market-derived rather than analytical. Kept
# lowercase; matching is case-insensitive. Deliberately narrow (2026-07-07
# line) — analytical sources (opta/xg/elo/fbref/...) are NOT in this list.
# NOTE: bare "spread" is deliberately absent — team news legitimately says
# "illness spread through the camp"; only market-specific phrasings count.
MARKET_WORDS = [
    "pinnacle", "kalshi", "polymarket", "draftkings", "fanduel", "betmgm",
    "bet365", "sport888", "888sport", "1xbet", "betfair", "smarkets",
    "novig", "william hill", "williamhill", "betway", "bovada", "betonline",
    "unibet", "caesars", "betfred", "oddschecker", "oddsportal",
    "the-odds-api", "sportsbook", "bookmaker", "book maker", "moneyline",
    "money line", "point spread", "spread line", "asian handicap",
    "handicap line", "over/under", "totals line", "odds of", "opened at",
    "priced at", "market price", "implied", "vig", "overround", "no-vig",
    "fair price", "closing line",
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


_MARKET_WORDS_RE = re.compile(
    "|".join(_word_pattern(w) for w in MARKET_WORDS), re.IGNORECASE)

# A bare number or percentage, e.g. "62%", "-145", "2.10". The lookbehind
# skips digits glued to letters (sport888, wtop365, promo slugs) — a price
# stands alone — and digits mid-number (no re-matching "65" out of "365").
_NUM_RE = re.compile(r"(?<![A-Za-z\d])[+-]?\d+(?:\.\d+)?%?")

# URLs: digits inside a URL are slug/promo characters, never a readable
# price (SKILL: URL-level exposure is a disclosure matter, not a number
# leak). Masked before number scanning.
_URL_RE = re.compile(r"https?://\S+")

# Date shapes are never a price: ISO dates ("2026-07-08", "2026/07/08"),
# bare years (1900-2099). Masked out before number detection so a book name
# next to an access date (the legitimate odds_exposure declaration format)
# does not trip the gate.
_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b")

# Markdown heading ordinals ("## 11. Odds exposure") are structural, not a
# price — even though a section titled "odds exposure" legitimately names
# market words (bookmaker/market) right next to its own number (found live
# 2026-07-16, m103_fra_eng_prelock.md: "## 11. Odds exposure (... bookmaker
# ...)" flagged "11"). Masked before number detection, same treatment as
# dates.
_HEADING_ORDINAL_RE = re.compile(r"(?m)^#{1,6}[ \t]*\d+(?=\.)")

# American odds (moneyline) shape: +150 / -110 / -1450. Flagged even with no
# book word nearby — EXCEPT when the nearby context is an analytical-rating
# phrase: Elo differences are written the same way ("Elo diff +404",
# "d_eff -146") and are core legitimate J input, not a price.
_AMERICAN_ODDS_RE = re.compile(r"(?<![\d.])[+-]\d{3,5}(?![\d.%])")
_RATING_CONTEXT_RE = re.compile(
    r"elo|rating|d_eff|diff|delta|評分|分差", re.IGNORECASE)

# Three decimal-odds-shaped numbers in a row (a 1x2 price triple), e.g.
# "2.10 3.30 3.80" or "2.10/3.30/3.80".
_ODDS_TRIPLE_RE = re.compile(
    r"\b([1-9]\d?\.\d{2})\s*[/,\-]?\s*([1-9]\d?\.\d{2})\s*[/,\-]?\s*"
    r"([1-9]\d?\.\d{2})\b")

PROXIMITY_CHARS = 60  # how close a number must sit to a market-word hit


def leaked_numbers(text):
    """Return the market-derived-looking (number, context) hits in `text`.

    Three patterns (Architecture Review v3 section 4.4): odds format (american
    odds shape, or a 1x2-style triple of decimal-odds numbers), OR any number
    sitting within PROXIMITY_CHARS of a market-source keyword. A number with
    no market-word nearby and no odds shape (an xG value, an Elo rating, a
    modeled probability) is not flagged."""
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
        lo, hi = max(0, m.start() - PROXIMITY_CHARS), min(len(masked), m.end() + PROXIMITY_CHARS)
        window = masked[lo:hi]
        if _RATING_CONTEXT_RE.search(window):
            continue  # Elo-diff / rating-delta notation, not a moneyline
        hits.append((m.group(0), window.strip()))
    for m in _ODDS_TRIPLE_RE.finditer(numscan):
        lo, hi = max(0, m.start() - PROXIMITY_CHARS), min(len(masked), m.end() + PROXIMITY_CHARS)
        hits.append((m.group(0), masked[lo:hi].strip()))
    for wm in _MARKET_WORDS_RE.finditer(masked):
        lo, hi = max(0, wm.start() - PROXIMITY_CHARS), min(len(masked), wm.end() + PROXIMITY_CHARS)
        for nm in _NUM_RE.finditer(numscan[lo:hi]):
            hits.append((nm.group(0), masked[lo:hi].strip()))
    seen, out = set(), []
    for num, ctx in hits:
        key = (num, ctx[:30])
        if key in seen:
            continue
        seen.add(key)
        out.append({"number": num, "context": ctx})
    return out


def validate_bundle_text(text):
    """The leak validator gate. Returns (ok, leaks). ok=False means this
    subagent output must be rejected and redone — never silently passed
    through to the analyst / J runner (Migration Plan Phase 0(c))."""
    leaks = leaked_numbers(text)
    return (len(leaks) == 0), leaks


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
