"""EPL 2026-27 fixtures: the whole season, matchweeks 1-38 (380 fixtures).

WHERE EACH PART COMES FROM
  MW1-5   hand-verified against two independent price-free sources, the
          official premierleague.com fixture article and ESPN's list.
          MW1's eight unrecorded kickoffs are all long past, so they stay
          date-only.
  MW6-38  captured 2026-10-06 in one pass from the same two sources. ESPN
          supplies the pairings per matchweek (numbered 1-38, original
          slots); the official article supplies the actual dates and times
          (broadcast selections applied). They were reconciled by program:
          380 distinct ordered pairs, ten fixtures and twenty clubs per
          week, weeks strictly ordered in time (`_selfcheck` re-asserts all
          of it). The same extraction also reproduced the 50 fixtures of
          MW1-5, which is how the method itself was checked.

WHAT `kickoff_utc` MEANS (read this before locking anything)
  lock_match() copies it into the lock permanently and refuses a fixture
  whose kickoff_utc is None, so a guessed time is never silently recorded.
  It is filled through MW9 only: the Premier League's 2026-08-17 notice
  finalised the live-broadcast selection for MW6-9 (and early November), so
  a printed time is taken as is and a Saturday listed TBC is the standard
  15:00 UK slot. MW10-38 are date only. Their times are provisional (picks
  not final; every midweek round shows an all-ten-at-20:00 placeholder), and
  even `date` can still move: the official page already carries Liverpool v
  Brighton moved Sat -> Sun 25 Oct after Brighton qualified for the
  Conference League.
  UK local -> UTC uses BST = last Sunday of March 01:00Z .. last Sunday of
  October 01:00Z. MW6-9 straddle the 2026-10-25 change, so for them (unlike
  MW1-5) the offset is not a constant -1h.

HOW TO REFRESH (before locking a matchweek, and again after any long gap)
  Re-read the official all-fixtures article AND the Premier League's
  "Fixture amendments ..." notices, plus ESPN's list; fill kickoff_utc for
  the weeks whose selection has become final; re-run `_selfcheck`. Lessons
  from building this:
  - Matchweek labels cannot be trusted through an extraction (an offset of
    +2 was once observed). Number a week from ESPN's pairing map and from
    date order, never from a heading.
  - One extraction of one source is not enough: a single pass dropped two
    pairings (ESPN's MW8), listed one fixture under two dates (the official
    page has Liverpool v Brighton on both Sat 24 and Sun 25 Oct), and
    re-emitted a five-fixture block under a wrong date. Two sources plus
    the invariants above are what expose that.
  - football-data.co.uk's fixtures.csv is not a schedule source (it holds
    only the next day or two) and each of its rows carries bookmaker odds,
    so reading it raw contaminates the analyst. Its PLAYED dates are fine
    for auditing past entries; that audit found two MW5 entries a day early
    (Leeds v Crystal Palace and Man City v Sunderland were played Sun 20
    Sep, not Sat 19), now corrected.
"""
from football_ai.competitions.epl_2026_27 import _MATCH_ID_PATTERN  # noqa: F401 (documents the id shape these ids follow)

# The 20 clubs of the 2026-27 Premier League, in football-data.co.uk's OWN
# spelling (E0.csv HomeTeam/AwayTeam values), which is the spelling every
# rating/lock call must use: `current_ratings()` keys off it, and a name
# that misses silently becomes a 1500.0 default rather than an error (the
# exact trap .claude/skills/epl-2026-27/SKILL.md warns about). Verified
# against the real E0_2627.csv team set on 2026-08-26 -- see
# `verify_team_names()` to re-run that check against live data.
ROSTER = frozenset({
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton",
    "Chelsea", "Coventry", "Crystal Palace", "Everton", "Fulham", "Hull",
    "Ipswich", "Leeds", "Liverpool", "Man City", "Man United", "Newcastle",
    "Nott'm Forest", "Sunderland", "Tottenham",
})

# The last matchweek whose kickoff_utc is confirmed (see the docstring).
KICKOFF_CONFIRMED_THROUGH = 9

# (gameweek, home, away, date, kickoff_utc|None).
# kickoff_utc is what lock_match() needs. None means "no confirmed kickoff":
# MW1 (kickoffs never captured, all long played) and MW10-38 (not yet
# confirmed; see the module docstring). `date` is the UK local date.
_MW1 = [
    (1, "Arsenal", "Coventry", "2026-08-21", "2026-08-21T19:00:00+00:00"),
    (1, "Hull", "Man United", "2026-08-22", None),
    (1, "Everton", "Crystal Palace", "2026-08-22", None),
    (1, "Ipswich", "Sunderland", "2026-08-22", None),
    (1, "Nott'm Forest", "Leeds", "2026-08-22", None),
    (1, "Brentford", "Tottenham", "2026-08-22", None),
    (1, "Brighton", "Aston Villa", "2026-08-23", None),
    (1, "Man City", "Bournemouth", "2026-08-23", "2026-08-23T14:00:00+00:00"),
    (1, "Newcastle", "Liverpool", "2026-08-23", None),
    (1, "Fulham", "Chelsea", "2026-08-24", None),
]

_MW2 = [
    (2, "Crystal Palace", "Man City", "2026-08-28", "2026-08-28T19:00:00+00:00"),
    (2, "Liverpool", "Nott'm Forest", "2026-08-29", "2026-08-29T11:30:00+00:00"),
    (2, "Bournemouth", "Everton", "2026-08-29", "2026-08-29T14:00:00+00:00"),
    (2, "Coventry", "Hull", "2026-08-29", "2026-08-29T14:00:00+00:00"),
    (2, "Tottenham", "Newcastle", "2026-08-29", "2026-08-29T16:30:00+00:00"),
    (2, "Chelsea", "Brighton", "2026-08-30", "2026-08-30T13:00:00+00:00"),
    (2, "Leeds", "Brentford", "2026-08-30", "2026-08-30T13:00:00+00:00"),
    (2, "Sunderland", "Fulham", "2026-08-30", "2026-08-30T13:00:00+00:00"),
    (2, "Man United", "Ipswich", "2026-08-30", "2026-08-30T15:30:00+00:00"),
    (2, "Aston Villa", "Arsenal", "2026-08-31", "2026-08-31T19:00:00+00:00"),
]

_MW3 = [
    (3, "Ipswich", "Liverpool", "2026-09-04", "2026-09-04T19:00:00+00:00"),
    (3, "Newcastle", "Bournemouth", "2026-09-05", "2026-09-05T11:30:00+00:00"),
    (3, "Brentford", "Sunderland", "2026-09-05", "2026-09-05T14:00:00+00:00"),
    (3, "Brighton", "Leeds", "2026-09-05", "2026-09-05T14:00:00+00:00"),
    (3, "Fulham", "Crystal Palace", "2026-09-05", "2026-09-05T14:00:00+00:00"),
    (3, "Man City", "Coventry", "2026-09-05", "2026-09-05T14:00:00+00:00"),
    (3, "Nott'm Forest", "Tottenham", "2026-09-05", "2026-09-05T14:00:00+00:00"),
    (3, "Hull", "Aston Villa", "2026-09-05", "2026-09-05T16:30:00+00:00"),
    (3, "Everton", "Man United", "2026-09-06", "2026-09-06T13:00:00+00:00"),
    (3, "Arsenal", "Chelsea", "2026-09-06", "2026-09-06T15:30:00+00:00"),
]

_MW4 = [
    (4, "Bournemouth", "Brentford", "2026-09-12", "2026-09-12T14:00:00+00:00"),
    (4, "Aston Villa", "Nott'm Forest", "2026-09-12", "2026-09-12T14:00:00+00:00"),
    (4, "Chelsea", "Hull", "2026-09-12", "2026-09-12T14:00:00+00:00"),
    (4, "Crystal Palace", "Ipswich", "2026-09-12", "2026-09-12T14:00:00+00:00"),
    (4, "Liverpool", "Fulham", "2026-09-12", "2026-09-12T14:00:00+00:00"),
    (4, "Tottenham", "Everton", "2026-09-12", "2026-09-12T16:30:00+00:00"),
    (4, "Sunderland", "Arsenal", "2026-09-12", "2026-09-12T19:00:00+00:00"),
    (4, "Coventry", "Brighton", "2026-09-13", "2026-09-13T13:00:00+00:00"),
    (4, "Man United", "Man City", "2026-09-13", "2026-09-13T15:30:00+00:00"),
    (4, "Leeds", "Newcastle", "2026-09-14", "2026-09-14T19:00:00+00:00"),
]

_MW5 = [
    (5, "Brentford", "Chelsea", "2026-09-18", "2026-09-18T19:00:00+00:00"),
    (5, "Tottenham", "Aston Villa", "2026-09-19", "2026-09-19T11:30:00+00:00"),
    (5, "Brighton", "Arsenal", "2026-09-19", "2026-09-19T14:00:00+00:00"),
    (5, "Everton", "Ipswich", "2026-09-19", "2026-09-19T14:00:00+00:00"),
    (5, "Leeds", "Crystal Palace", "2026-09-20", "2026-09-20T13:00:00+00:00"),
    (5, "Man City", "Sunderland", "2026-09-20", "2026-09-20T13:00:00+00:00"),
    (5, "Newcastle", "Hull", "2026-09-19", "2026-09-19T14:00:00+00:00"),
    (5, "Nott'm Forest", "Coventry", "2026-09-19", "2026-09-19T16:30:00+00:00"),
    (5, "Bournemouth", "Liverpool", "2026-09-20", "2026-09-20T13:00:00+00:00"),
    (5, "Fulham", "Man United", "2026-09-20", "2026-09-20T15:30:00+00:00"),
]

_MW6 = [
    (6, "Arsenal", "Leeds", "2026-10-10", "2026-10-10T11:30:00+00:00"),
    (6, "Man United", "Tottenham", "2026-10-10", "2026-10-10T16:30:00+00:00"),
    (6, "Aston Villa", "Brentford", "2026-10-10", "2026-10-10T14:00:00+00:00"),
    (6, "Chelsea", "Bournemouth", "2026-10-10", "2026-10-10T14:00:00+00:00"),
    (6, "Ipswich", "Fulham", "2026-10-10", "2026-10-10T14:00:00+00:00"),
    (6, "Sunderland", "Brighton", "2026-10-10", "2026-10-10T14:00:00+00:00"),
    (6, "Crystal Palace", "Nott'm Forest", "2026-10-11", "2026-10-11T13:00:00+00:00"),
    (6, "Hull", "Everton", "2026-10-11", "2026-10-11T13:00:00+00:00"),
    (6, "Liverpool", "Man City", "2026-10-11", "2026-10-11T15:30:00+00:00"),
    (6, "Coventry", "Newcastle", "2026-10-12", "2026-10-12T19:00:00+00:00"),
]

_MW7 = [
    (7, "Everton", "Chelsea", "2026-10-17", "2026-10-17T11:30:00+00:00"),
    (7, "Newcastle", "Aston Villa", "2026-10-17", "2026-10-17T16:30:00+00:00"),
    (7, "Brentford", "Liverpool", "2026-10-17", "2026-10-17T14:00:00+00:00"),
    (7, "Fulham", "Hull", "2026-10-17", "2026-10-17T14:00:00+00:00"),
    (7, "Man City", "Ipswich", "2026-10-17", "2026-10-17T14:00:00+00:00"),
    (7, "Bournemouth", "Sunderland", "2026-10-18", "2026-10-18T13:00:00+00:00"),
    (7, "Brighton", "Crystal Palace", "2026-10-18", "2026-10-18T13:00:00+00:00"),
    (7, "Leeds", "Man United", "2026-10-18", "2026-10-18T13:00:00+00:00"),
    (7, "Nott'm Forest", "Arsenal", "2026-10-18", "2026-10-18T15:30:00+00:00"),
    (7, "Tottenham", "Coventry", "2026-10-19", "2026-10-19T19:00:00+00:00"),
]

_MW8 = [
    (8, "Ipswich", "Nott'm Forest", "2026-10-23", "2026-10-23T19:00:00+00:00"),
    (8, "Aston Villa", "Man City", "2026-10-24", "2026-10-24T11:30:00+00:00"),
    (8, "Chelsea", "Tottenham", "2026-10-24", "2026-10-24T16:30:00+00:00"),
    (8, "Arsenal", "Everton", "2026-10-24", "2026-10-24T14:00:00+00:00"),
    (8, "Coventry", "Fulham", "2026-10-24", "2026-10-24T14:00:00+00:00"),
    (8, "Crystal Palace", "Newcastle", "2026-10-25", "2026-10-25T14:00:00+00:00"),
    (8, "Hull", "Brentford", "2026-10-25", "2026-10-25T14:00:00+00:00"),
    (8, "Liverpool", "Brighton", "2026-10-25", "2026-10-25T14:00:00+00:00"),
    (8, "Man United", "Bournemouth", "2026-10-25", "2026-10-25T14:00:00+00:00"),
    (8, "Sunderland", "Leeds", "2026-10-25", "2026-10-25T16:30:00+00:00"),
]

_MW9 = [
    (9, "Chelsea", "Man United", "2026-10-31", "2026-10-31T12:30:00+00:00"),
    (9, "Tottenham", "Crystal Palace", "2026-10-31", "2026-10-31T17:30:00+00:00"),
    (9, "Aston Villa", "Fulham", "2026-10-31", "2026-10-31T20:00:00+00:00"),
    (9, "Bournemouth", "Leeds", "2026-10-31", "2026-10-31T15:00:00+00:00"),
    (9, "Brentford", "Nott'm Forest", "2026-10-31", "2026-10-31T15:00:00+00:00"),
    (9, "Coventry", "Sunderland", "2026-10-31", "2026-10-31T15:00:00+00:00"),
    (9, "Hull", "Ipswich", "2026-10-31", "2026-10-31T15:00:00+00:00"),
    (9, "Man City", "Brighton", "2026-10-31", "2026-10-31T15:00:00+00:00"),
    (9, "Liverpool", "Arsenal", "2026-11-01", "2026-11-01T16:30:00+00:00"),
    (9, "Newcastle", "Everton", "2026-11-02", "2026-11-02T20:00:00+00:00"),
]

_MW10 = [
    (10, "Everton", "Coventry", "2026-11-06", None),
    (10, "Leeds", "Tottenham", "2026-11-07", None),
    (10, "Nott'm Forest", "Man City", "2026-11-07", None),
    (10, "Arsenal", "Hull", "2026-11-07", None),
    (10, "Fulham", "Newcastle", "2026-11-07", None),
    (10, "Brighton", "Brentford", "2026-11-08", None),
    (10, "Crystal Palace", "Liverpool", "2026-11-08", None),
    (10, "Ipswich", "Bournemouth", "2026-11-08", None),
    (10, "Sunderland", "Chelsea", "2026-11-08", None),
    (10, "Man United", "Aston Villa", "2026-11-08", None),
]

_MW11 = [
    (11, "Man City", "Fulham", "2026-11-21", None),
    (11, "Newcastle", "Arsenal", "2026-11-21", None),
    (11, "Aston Villa", "Sunderland", "2026-11-21", None),
    (11, "Bournemouth", "Nott'm Forest", "2026-11-21", None),
    (11, "Chelsea", "Leeds", "2026-11-21", None),
    (11, "Coventry", "Crystal Palace", "2026-11-21", None),
    (11, "Tottenham", "Ipswich", "2026-11-21", None),
    (11, "Hull", "Brighton", "2026-11-22", None),
    (11, "Liverpool", "Man United", "2026-11-22", None),
    (11, "Brentford", "Everton", "2026-11-23", None),
]

_MW12 = [
    (12, "Nott'm Forest", "Chelsea", "2026-11-27", None),
    (12, "Ipswich", "Aston Villa", "2026-11-28", None),
    (12, "Leeds", "Coventry", "2026-11-28", None),
    (12, "Man United", "Brentford", "2026-11-28", None),
    (12, "Everton", "Liverpool", "2026-11-29", None),
    (12, "Brighton", "Newcastle", "2026-11-29", None),
    (12, "Crystal Palace", "Hull", "2026-11-29", None),
    (12, "Fulham", "Bournemouth", "2026-11-29", None),
    (12, "Sunderland", "Tottenham", "2026-11-29", None),
    (12, "Arsenal", "Man City", "2026-11-29", None),
]

_MW13 = [
    (13, "Aston Villa", "Everton", "2026-12-02", None),
    (13, "Bournemouth", "Brighton", "2026-12-02", None),
    (13, "Brentford", "Arsenal", "2026-12-02", None),
    (13, "Chelsea", "Crystal Palace", "2026-12-02", None),
    (13, "Coventry", "Ipswich", "2026-12-02", None),
    (13, "Hull", "Nott'm Forest", "2026-12-02", None),
    (13, "Liverpool", "Sunderland", "2026-12-02", None),
    (13, "Man City", "Leeds", "2026-12-02", None),
    (13, "Newcastle", "Man United", "2026-12-02", None),
    (13, "Tottenham", "Fulham", "2026-12-02", None),
]

_MW14 = [
    (14, "Aston Villa", "Crystal Palace", "2026-12-05", None),
    (14, "Bournemouth", "Hull", "2026-12-05", None),
    (14, "Brentford", "Man City", "2026-12-05", None),
    (14, "Chelsea", "Liverpool", "2026-12-05", None),
    (14, "Everton", "Fulham", "2026-12-05", None),
    (14, "Leeds", "Ipswich", "2026-12-05", None),
    (14, "Man United", "Coventry", "2026-12-05", None),
    (14, "Newcastle", "Sunderland", "2026-12-05", None),
    (14, "Nott'm Forest", "Brighton", "2026-12-05", None),
    (14, "Tottenham", "Arsenal", "2026-12-05", None),
]

_MW15 = [
    (15, "Arsenal", "Bournemouth", "2026-12-12", None),
    (15, "Brighton", "Everton", "2026-12-12", None),
    (15, "Coventry", "Aston Villa", "2026-12-12", None),
    (15, "Crystal Palace", "Man United", "2026-12-12", None),
    (15, "Fulham", "Brentford", "2026-12-12", None),
    (15, "Hull", "Tottenham", "2026-12-12", None),
    (15, "Ipswich", "Newcastle", "2026-12-12", None),
    (15, "Liverpool", "Leeds", "2026-12-12", None),
    (15, "Man City", "Chelsea", "2026-12-12", None),
    (15, "Sunderland", "Nott'm Forest", "2026-12-12", None),
]

_MW16 = [
    (16, "Arsenal", "Man United", "2026-12-19", None),
    (16, "Bournemouth", "Coventry", "2026-12-19", None),
    (16, "Brentford", "Newcastle", "2026-12-19", None),
    (16, "Brighton", "Ipswich", "2026-12-19", None),
    (16, "Chelsea", "Aston Villa", "2026-12-19", None),
    (16, "Leeds", "Fulham", "2026-12-19", None),
    (16, "Liverpool", "Tottenham", "2026-12-19", None),
    (16, "Man City", "Hull", "2026-12-19", None),
    (16, "Nott'm Forest", "Everton", "2026-12-19", None),
    (16, "Sunderland", "Crystal Palace", "2026-12-19", None),
]

_MW17 = [
    (17, "Aston Villa", "Leeds", "2026-12-26", None),
    (17, "Hull", "Liverpool", "2026-12-26", None),
    (17, "Newcastle", "Man City", "2026-12-26", None),
    (17, "Everton", "Sunderland", "2026-12-26", None),
    (17, "Fulham", "Brighton", "2026-12-26", None),
    (17, "Ipswich", "Brentford", "2026-12-26", None),
    (17, "Tottenham", "Bournemouth", "2026-12-26", None),
    (17, "Coventry", "Chelsea", "2026-12-27", None),
    (17, "Man United", "Nott'm Forest", "2026-12-27", None),
    (17, "Crystal Palace", "Arsenal", "2026-12-27", None),
]

_MW18 = [
    (18, "Hull", "Leeds", "2026-12-29", None),
    (18, "Tottenham", "Brighton", "2026-12-29", None),
    (18, "Everton", "Man City", "2026-12-29", None),
    (18, "Coventry", "Brentford", "2026-12-30", None),
    (18, "Crystal Palace", "Bournemouth", "2026-12-30", None),
    (18, "Fulham", "Arsenal", "2026-12-30", None),
    (18, "Man United", "Sunderland", "2026-12-30", None),
    (18, "Newcastle", "Nott'm Forest", "2026-12-30", None),
    (18, "Aston Villa", "Liverpool", "2026-12-30", None),
    (18, "Ipswich", "Chelsea", "2026-12-30", None),
]

_MW19 = [
    (19, "Leeds", "Everton", "2027-01-01", None),
    (19, "Brighton", "Man United", "2027-01-02", None),
    (19, "Bournemouth", "Aston Villa", "2027-01-02", None),
    (19, "Arsenal", "Ipswich", "2027-01-02", None),
    (19, "Brentford", "Crystal Palace", "2027-01-02", None),
    (19, "Liverpool", "Coventry", "2027-01-02", None),
    (19, "Nott'm Forest", "Fulham", "2027-01-02", None),
    (19, "Sunderland", "Hull", "2027-01-02", None),
    (19, "Chelsea", "Newcastle", "2027-01-03", None),
    (19, "Man City", "Tottenham", "2027-01-03", None),
]

_MW20 = [
    (20, "Arsenal", "Brentford", "2027-01-05", None),
    (20, "Brighton", "Bournemouth", "2027-01-05", None),
    (20, "Everton", "Aston Villa", "2027-01-05", None),
    (20, "Nott'm Forest", "Hull", "2027-01-05", None),
    (20, "Sunderland", "Liverpool", "2027-01-05", None),
    (20, "Crystal Palace", "Chelsea", "2027-01-06", None),
    (20, "Fulham", "Tottenham", "2027-01-06", None),
    (20, "Ipswich", "Coventry", "2027-01-06", None),
    (20, "Leeds", "Man City", "2027-01-06", None),
    (20, "Man United", "Newcastle", "2027-01-07", None),
]

_MW21 = [
    (21, "Aston Villa", "Man United", "2027-01-16", None),
    (21, "Bournemouth", "Ipswich", "2027-01-16", None),
    (21, "Brentford", "Brighton", "2027-01-16", None),
    (21, "Chelsea", "Sunderland", "2027-01-16", None),
    (21, "Coventry", "Everton", "2027-01-16", None),
    (21, "Hull", "Arsenal", "2027-01-16", None),
    (21, "Liverpool", "Crystal Palace", "2027-01-16", None),
    (21, "Man City", "Nott'm Forest", "2027-01-16", None),
    (21, "Newcastle", "Fulham", "2027-01-16", None),
    (21, "Tottenham", "Leeds", "2027-01-16", None),
]

_MW22 = [
    (22, "Arsenal", "Newcastle", "2027-01-23", None),
    (22, "Brighton", "Man City", "2027-01-23", None),
    (22, "Crystal Palace", "Tottenham", "2027-01-23", None),
    (22, "Everton", "Brentford", "2027-01-23", None),
    (22, "Fulham", "Aston Villa", "2027-01-23", None),
    (22, "Ipswich", "Hull", "2027-01-23", None),
    (22, "Leeds", "Chelsea", "2027-01-23", None),
    (22, "Man United", "Liverpool", "2027-01-23", None),
    (22, "Nott'm Forest", "Bournemouth", "2027-01-23", None),
    (22, "Sunderland", "Coventry", "2027-01-23", None),
]

_MW23 = [
    (23, "Aston Villa", "Ipswich", "2027-01-30", None),
    (23, "Bournemouth", "Fulham", "2027-01-30", None),
    (23, "Brentford", "Man United", "2027-01-30", None),
    (23, "Chelsea", "Nott'm Forest", "2027-01-30", None),
    (23, "Coventry", "Leeds", "2027-01-30", None),
    (23, "Hull", "Crystal Palace", "2027-01-30", None),
    (23, "Liverpool", "Everton", "2027-01-30", None),
    (23, "Man City", "Arsenal", "2027-01-30", None),
    (23, "Newcastle", "Brighton", "2027-01-30", None),
    (23, "Tottenham", "Sunderland", "2027-01-30", None),
]

_MW24 = [
    (24, "Arsenal", "Liverpool", "2027-02-06", None),
    (24, "Brighton", "Hull", "2027-02-06", None),
    (24, "Crystal Palace", "Coventry", "2027-02-06", None),
    (24, "Everton", "Newcastle", "2027-02-06", None),
    (24, "Fulham", "Man City", "2027-02-06", None),
    (24, "Ipswich", "Tottenham", "2027-02-06", None),
    (24, "Leeds", "Bournemouth", "2027-02-06", None),
    (24, "Man United", "Chelsea", "2027-02-06", None),
    (24, "Nott'm Forest", "Brentford", "2027-02-06", None),
    (24, "Sunderland", "Aston Villa", "2027-02-06", None),
]

_MW25 = [
    (25, "Aston Villa", "Bournemouth", "2027-02-10", None),
    (25, "Coventry", "Liverpool", "2027-02-10", None),
    (25, "Crystal Palace", "Brentford", "2027-02-10", None),
    (25, "Everton", "Leeds", "2027-02-10", None),
    (25, "Fulham", "Nott'm Forest", "2027-02-10", None),
    (25, "Hull", "Sunderland", "2027-02-10", None),
    (25, "Ipswich", "Arsenal", "2027-02-10", None),
    (25, "Man United", "Brighton", "2027-02-10", None),
    (25, "Newcastle", "Chelsea", "2027-02-10", None),
    (25, "Tottenham", "Man City", "2027-02-10", None),
]

_MW26 = [
    (26, "Arsenal", "Fulham", "2027-02-20", None),
    (26, "Bournemouth", "Crystal Palace", "2027-02-20", None),
    (26, "Brentford", "Coventry", "2027-02-20", None),
    (26, "Brighton", "Tottenham", "2027-02-20", None),
    (26, "Chelsea", "Ipswich", "2027-02-20", None),
    (26, "Leeds", "Aston Villa", "2027-02-20", None),
    (26, "Liverpool", "Hull", "2027-02-20", None),
    (26, "Man City", "Newcastle", "2027-02-20", None),
    (26, "Nott'm Forest", "Man United", "2027-02-20", None),
    (26, "Sunderland", "Everton", "2027-02-20", None),
]

_MW27 = [
    (27, "Aston Villa", "Chelsea", "2027-02-27", None),
    (27, "Coventry", "Bournemouth", "2027-02-27", None),
    (27, "Crystal Palace", "Sunderland", "2027-02-27", None),
    (27, "Everton", "Nott'm Forest", "2027-02-27", None),
    (27, "Fulham", "Leeds", "2027-02-27", None),
    (27, "Hull", "Man City", "2027-02-27", None),
    (27, "Ipswich", "Brighton", "2027-02-27", None),
    (27, "Man United", "Arsenal", "2027-02-27", None),
    (27, "Newcastle", "Brentford", "2027-02-27", None),
    (27, "Tottenham", "Liverpool", "2027-02-27", None),
]

_MW28 = [
    (28, "Arsenal", "Crystal Palace", "2027-03-03", None),
    (28, "Bournemouth", "Tottenham", "2027-03-03", None),
    (28, "Brentford", "Ipswich", "2027-03-03", None),
    (28, "Brighton", "Fulham", "2027-03-03", None),
    (28, "Chelsea", "Coventry", "2027-03-03", None),
    (28, "Leeds", "Hull", "2027-03-03", None),
    (28, "Liverpool", "Aston Villa", "2027-03-03", None),
    (28, "Man City", "Everton", "2027-03-03", None),
    (28, "Nott'm Forest", "Newcastle", "2027-03-03", None),
    (28, "Sunderland", "Man United", "2027-03-03", None),
]

_MW29 = [
    (29, "Aston Villa", "Hull", "2027-03-13", None),
    (29, "Bournemouth", "Newcastle", "2027-03-13", None),
    (29, "Chelsea", "Arsenal", "2027-03-13", None),
    (29, "Coventry", "Man City", "2027-03-13", None),
    (29, "Crystal Palace", "Fulham", "2027-03-13", None),
    (29, "Leeds", "Brighton", "2027-03-13", None),
    (29, "Liverpool", "Ipswich", "2027-03-13", None),
    (29, "Man United", "Everton", "2027-03-13", None),
    (29, "Sunderland", "Brentford", "2027-03-13", None),
    (29, "Tottenham", "Nott'm Forest", "2027-03-13", None),
]

_MW30 = [
    (30, "Arsenal", "Sunderland", "2027-03-20", None),
    (30, "Brentford", "Bournemouth", "2027-03-20", None),
    (30, "Brighton", "Coventry", "2027-03-20", None),
    (30, "Everton", "Tottenham", "2027-03-20", None),
    (30, "Fulham", "Liverpool", "2027-03-20", None),
    (30, "Hull", "Chelsea", "2027-03-20", None),
    (30, "Ipswich", "Crystal Palace", "2027-03-20", None),
    (30, "Man City", "Man United", "2027-03-20", None),
    (30, "Newcastle", "Leeds", "2027-03-20", None),
    (30, "Nott'm Forest", "Aston Villa", "2027-03-20", None),
]

_MW31 = [
    (31, "Aston Villa", "Brighton", "2027-04-10", None),
    (31, "Bournemouth", "Man City", "2027-04-10", None),
    (31, "Chelsea", "Fulham", "2027-04-10", None),
    (31, "Coventry", "Arsenal", "2027-04-10", None),
    (31, "Crystal Palace", "Everton", "2027-04-10", None),
    (31, "Leeds", "Nott'm Forest", "2027-04-10", None),
    (31, "Liverpool", "Newcastle", "2027-04-10", None),
    (31, "Man United", "Hull", "2027-04-10", None),
    (31, "Sunderland", "Ipswich", "2027-04-10", None),
    (31, "Tottenham", "Brentford", "2027-04-10", None),
]

_MW32 = [
    (32, "Arsenal", "Aston Villa", "2027-04-17", None),
    (32, "Brentford", "Leeds", "2027-04-17", None),
    (32, "Brighton", "Chelsea", "2027-04-17", None),
    (32, "Everton", "Bournemouth", "2027-04-17", None),
    (32, "Fulham", "Sunderland", "2027-04-17", None),
    (32, "Hull", "Coventry", "2027-04-17", None),
    (32, "Ipswich", "Man United", "2027-04-17", None),
    (32, "Man City", "Crystal Palace", "2027-04-17", None),
    (32, "Newcastle", "Tottenham", "2027-04-17", None),
    (32, "Nott'm Forest", "Liverpool", "2027-04-17", None),
]

_MW33 = [
    (33, "Aston Villa", "Coventry", "2027-04-24", None),
    (33, "Bournemouth", "Arsenal", "2027-04-24", None),
    (33, "Brentford", "Fulham", "2027-04-24", None),
    (33, "Chelsea", "Man City", "2027-04-24", None),
    (33, "Everton", "Brighton", "2027-04-24", None),
    (33, "Leeds", "Liverpool", "2027-04-24", None),
    (33, "Man United", "Crystal Palace", "2027-04-24", None),
    (33, "Newcastle", "Ipswich", "2027-04-24", None),
    (33, "Nott'm Forest", "Sunderland", "2027-04-24", None),
    (33, "Tottenham", "Hull", "2027-04-24", None),
]

_MW34 = [
    (34, "Arsenal", "Tottenham", "2027-05-01", None),
    (34, "Brighton", "Nott'm Forest", "2027-05-01", None),
    (34, "Coventry", "Man United", "2027-05-01", None),
    (34, "Crystal Palace", "Aston Villa", "2027-05-01", None),
    (34, "Fulham", "Everton", "2027-05-01", None),
    (34, "Hull", "Bournemouth", "2027-05-01", None),
    (34, "Ipswich", "Leeds", "2027-05-01", None),
    (34, "Liverpool", "Chelsea", "2027-05-01", None),
    (34, "Man City", "Brentford", "2027-05-01", None),
    (34, "Sunderland", "Newcastle", "2027-05-01", None),
]

_MW35 = [
    (35, "Bournemouth", "Man United", "2027-05-08", None),
    (35, "Brentford", "Aston Villa", "2027-05-08", None),
    (35, "Brighton", "Sunderland", "2027-05-08", None),
    (35, "Everton", "Hull", "2027-05-08", None),
    (35, "Fulham", "Ipswich", "2027-05-08", None),
    (35, "Leeds", "Arsenal", "2027-05-08", None),
    (35, "Man City", "Liverpool", "2027-05-08", None),
    (35, "Newcastle", "Coventry", "2027-05-08", None),
    (35, "Nott'm Forest", "Crystal Palace", "2027-05-08", None),
    (35, "Tottenham", "Chelsea", "2027-05-08", None),
]

_MW36 = [
    (36, "Arsenal", "Nott'm Forest", "2027-05-15", None),
    (36, "Aston Villa", "Newcastle", "2027-05-15", None),
    (36, "Chelsea", "Everton", "2027-05-15", None),
    (36, "Coventry", "Tottenham", "2027-05-15", None),
    (36, "Crystal Palace", "Brighton", "2027-05-15", None),
    (36, "Hull", "Fulham", "2027-05-15", None),
    (36, "Ipswich", "Man City", "2027-05-15", None),
    (36, "Liverpool", "Brentford", "2027-05-15", None),
    (36, "Man United", "Leeds", "2027-05-15", None),
    (36, "Sunderland", "Bournemouth", "2027-05-15", None),
]

_MW37 = [
    (37, "Bournemouth", "Chelsea", "2027-05-23", None),
    (37, "Brentford", "Hull", "2027-05-23", None),
    (37, "Brighton", "Liverpool", "2027-05-23", None),
    (37, "Everton", "Arsenal", "2027-05-23", None),
    (37, "Fulham", "Coventry", "2027-05-23", None),
    (37, "Leeds", "Sunderland", "2027-05-23", None),
    (37, "Man City", "Aston Villa", "2027-05-23", None),
    (37, "Newcastle", "Crystal Palace", "2027-05-23", None),
    (37, "Nott'm Forest", "Ipswich", "2027-05-23", None),
    (37, "Tottenham", "Man United", "2027-05-23", None),
]

_MW38 = [
    (38, "Arsenal", "Brighton", "2027-05-30", None),
    (38, "Aston Villa", "Tottenham", "2027-05-30", None),
    (38, "Chelsea", "Brentford", "2027-05-30", None),
    (38, "Coventry", "Nott'm Forest", "2027-05-30", None),
    (38, "Crystal Palace", "Leeds", "2027-05-30", None),
    (38, "Hull", "Newcastle", "2027-05-30", None),
    (38, "Ipswich", "Everton", "2027-05-30", None),
    (38, "Liverpool", "Bournemouth", "2027-05-30", None),
    (38, "Man United", "Fulham", "2027-05-30", None),
    (38, "Sunderland", "Man City", "2027-05-30", None),
]

_ALL = (_MW1
        + _MW2
        + _MW3
        + _MW4
        + _MW5
        + _MW6
        + _MW7
        + _MW8
        + _MW9
        + _MW10
        + _MW11
        + _MW12
        + _MW13
        + _MW14
        + _MW15
        + _MW16
        + _MW17
        + _MW18
        + _MW19
        + _MW20
        + _MW21
        + _MW22
        + _MW23
        + _MW24
        + _MW25
        + _MW26
        + _MW27
        + _MW28
        + _MW29
        + _MW30
        + _MW31
        + _MW32
        + _MW33
        + _MW34
        + _MW35
        + _MW36
        + _MW37
        + _MW38)


def _abbr(team, n=3):
    return "".join(c for c in team.lower() if c.isalnum())[:n]


def match_id(gw, home, away):
    """Three letters per club, widened only when three would collide.

    BOTH Manchester clubs abbreviate to "man" (manunited / mancity), which
    is survivable while they play other people -- the opponent's letters
    disambiguate, and `_selfcheck` asserts global uniqueness rather than
    trusting that. The derby breaks it: matchweek 4's Manchester United v
    Manchester City would be `..._man_man`, a unique string that names
    neither club.

    So the widening is CONDITIONAL: only when the two three-letter forms
    are equal do both sides widen (to `manu` / `manc`). This is deliberate
    rather than switching everything to four letters -- gameweeks 1-3 are
    already locked in the store under the three-letter convention, and
    silently re-keying settled fixtures to make a naming scheme prettier is
    exactly the kind of history rewrite this project forbids. No existing
    fixture has equal abbreviations, so no existing id changes.

    Even so: the id is a key, not a label. Read `home_team`/`away_team`
    when you need to know who is playing."""
    h, a = _abbr(home), _abbr(away)
    if h == a:
        h, a = _abbr(home, 4), _abbr(away, 4)
    return f"epl2627_gw{gw:02d}_{h}_{a}"


FIXTURES = [
    {"match_id": match_id(gw, home, away), "gameweek": gw,
     "home_team": home, "away_team": away, "date": date,
     "kickoff_utc": ko}
    for gw, home, away, date, ko in _ALL
]


def by_gameweek(gw):
    """The ten fixtures of one matchweek, in kickoff order."""
    return [f for f in FIXTURES if f["gameweek"] == gw]


def verify_team_names(start_year=2026):
    """Cross-check ROSTER against the live football-data.co.uk team set for
    `start_year`'s season. Returns a list of complaint strings (empty =
    clean). NOT part of `_selfcheck` -- it touches the network on a cold
    cache, and the selftest suite is offline by design."""
    from football_ai.data.football_data_co_uk import load_seasons
    rows = load_seasons("E0", [start_year])
    seen = {r["home_team"] for r in rows} | {r["away_team"] for r in rows}
    if not seen:
        return [f"no played fixtures yet for {start_year}-{start_year + 1}; "
                f"cannot verify names against results"]
    out = []
    for name in sorted(seen - set(ROSTER)):
        out.append(f"data source has {name!r}, missing from ROSTER")
    for name in sorted(set(ROSTER) - seen):
        out.append(f"ROSTER has {name!r}, not seen in the data source yet "
                   f"(fine early in a season if that club has not played)")
    return out


def _selfcheck():
    from datetime import datetime

    fails = []

    gws = sorted({f["gameweek"] for f in FIXTURES})
    if gws != list(range(1, 39)):
        fails.append(f"expected matchweeks [1..38], got {gws}")
    if len(FIXTURES) != 380:
        fails.append(f"a 20-club double round robin has 380 fixtures, "
                     f"got {len(FIXTURES)}")
    # every club hosts every other club exactly once: a transcription that
    # swaps home/away or drops a pairing leaves a hole here even when each
    # individual matchweek still looks like twenty distinct clubs
    full = {(h, a) for h in ROSTER for a in ROSTER if h != a}
    have = {(f["home_team"], f["away_team"]) for f in FIXTURES}
    if full - have:
        fails.append(f"ordered pairs missing from the season: "
                     f"{sorted(full - have)[:5]} ...")
    if have - full:
        fails.append(f"fixtures between non-ROSTER clubs: "
                     f"{sorted(have - full)[:5]}")

    # A league pairing happens exactly ONCE per direction per season. A
    # duplicate here means a transcription put the same fixture in two
    # matchweeks -- the single most likely error when hand-copying a
    # schedule, and invisible without this check.
    pairs = [(f["home_team"], f["away_team"]) for f in FIXTURES]
    dupes = sorted({p for p in pairs if pairs.count(p) > 1})
    if dupes:
        fails.append(f"the same ordered fixture appears more than once: {dupes}")

    # the derby: equal three-letter forms must widen, and only then
    derby = match_id(4, "Man United", "Man City")
    if derby != "epl2627_gw04_manu_manc":
        fails.append(f"colliding abbreviations must widen to four letters, "
                     f"got {derby!r}")
    if match_id(2, "Man United", "Ipswich") != "epl2627_gw02_man_ips":
        fails.append("a non-colliding pair must keep the three-letter form "
                     "— widening it would re-key already-locked fixtures")

    for gw in gws:
        wk = by_gameweek(gw)
        if len(wk) != 10:
            fails.append(f"matchweek {gw} should have 10 fixtures, got {len(wk)}")
        teams = [t for f in wk for t in (f["home_team"], f["away_team"])]
        if len(teams) != len(set(teams)):
            fails.append(f"matchweek {gw}: a club appears twice")
        stray = sorted(set(teams) - ROSTER)
        if stray:
            fails.append(f"matchweek {gw}: club(s) not in ROSTER "
                         f"(silent 1500-Elo trap): {stray}")
        if len(set(teams)) != 20:
            fails.append(f"matchweek {gw} should cover all 20 clubs, "
                         f"got {len(set(teams))}")

    ids = [f["match_id"] for f in FIXTURES]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        fails.append(f"match_id collision across the whole fixture list: {dupes}")

    if match_id(1, "Arsenal", "Coventry") != "epl2627_gw01_ars_cov":
        fails.append(f"match_id() shape drifted: "
                     f"{match_id(1, 'Arsenal', 'Coventry')!r}")
    # the two already-locked MW1 fixtures must keep resolving to the ids the
    # store is keyed on -- a change here would orphan real locks
    if match_id(1, "Man City", "Bournemouth") != "epl2627_gw01_man_bou":
        fails.append("match_id() no longer reproduces the stored gw01_man_bou id")

    # matchweeks must not overlap in time: a date typed into the wrong week
    # shows up as a week that starts before the previous one ends
    span = {}
    for f in FIXTURES:
        lo, hi = span.get(f["gameweek"], (f["date"], f["date"]))
        span[f["gameweek"]] = (min(lo, f["date"]), max(hi, f["date"]))
    for gw in gws[1:]:
        if gw - 1 in span and span[gw][0] <= span[gw - 1][1]:
            fails.append(f"matchweek {gw} starts {span[gw][0]} but matchweek "
                         f"{gw - 1} runs to {span[gw - 1][1]}")

    for f in FIXTURES:
        ko = f["kickoff_utc"]
        if ko is None:
            if 2 <= f["gameweek"] <= KICKOFF_CONFIRMED_THROUGH:
                fails.append(f"{f['match_id']}: kickoff_utc is required for "
                             f"MW2-{KICKOFF_CONFIRMED_THROUGH}")
            continue
        try:
            dt = datetime.fromisoformat(ko)
        except ValueError:
            fails.append(f"{f['match_id']}: unparseable kickoff_utc {ko!r}")
            continue
        if dt.tzinfo is None:
            fails.append(f"{f['match_id']}: kickoff_utc must be tz-aware, got {ko!r}")
        elif not ko.startswith(f["date"]):
            fails.append(f"{f['match_id']}: kickoff_utc {ko!r} disagrees "
                         f"with date {f['date']!r}")

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
