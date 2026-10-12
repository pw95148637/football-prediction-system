"""`python -m football_ai.competitions` -- the session-opening report in
CLAUDE.md: what is due across every ACTIVE (non-archived) competition, then
the most urgent fixture and its next legal step (see `opening_report`).
`python -m football_ai.core.agenda` only checks the archived WC2026 path."""
from football_ai.competitions import opening_report

if __name__ == "__main__":
    print("\n".join(opening_report()))
