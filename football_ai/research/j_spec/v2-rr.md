# J_SPEC v2-rr — round-robin league adaptation of v2 (2026-08-26)

> This is NOT a successor to v2. It is v2 applied to a domestic
> round-robin league (the EPL 2026-27 window, `era=v1-epl-2026-27`), and
> it changes only what the knockout format made unstatable. v1 and v2 are
> frozen and untouched; a knockout window keeps using `v2`. Everything in
> v1 and v2 not contradicted below is binding here unchanged — doctrine,
> the two levers, the decomposition rule, forbidden reasoning, JSON
> discipline, the k-sample runner mechanics.

## 1. There is no advancement market

v1 requires a `qualify_home` field: the probability the home side goes
through. A league fixture has no tie, no second leg and nothing to go
through to, so M does not carry that field either.

**OMIT `qualify_home` from your output entirely.** Do not estimate one,
do not emit `null`. A `qualify_home` key in this competition's output is
a validation failure, not a rounding issue — it would mean the instrument
answered a question nobody asked.

## 2. Ninety minutes is the whole match

No extra time, no shoot-out. v1's caveat that extra time deflates the
observed draw rate does not apply: a draw here is a final, settled
outcome that the market prices directly. Reason about the draw as a real
result, not as a way-station to a tiebreak.

## 3. Home advantage is already in M — do not re-add it

The knockout window's fixtures were neutral-venue by default. This one is
not: M's `inputs.elo_diff_effective` ALREADY includes this competition's
fitted home-advantage constant on top of the raw Elo difference. "Playing
at home" is therefore never an available factor. A venue-specific fact
that the constant cannot know (a closed stand, a pitch relaid last week,
a fixture moved to a neutral ground) is still legitimate — but it must be
that specific fact, evidenced, not the generic home edge.

## 4. The bar for "the market has not priced this" is higher here

A tournament tie is a one-off between sides that may not have met in
years. A league fixture sits inside a heavily traded, weekly market with
full public team news, and both clubs have been continuously priced all
season. Information genuinely unavailable to that market is rarer than it
was in the knockout window. This does not forbid deviation; it means the
absorption case for every factor has to be correspondingly stronger, and
that "empty factors, J = M" is the expected outcome more often than not.

## 5. Everything else

Output schema is v1's minus `qualify_home`, plus v2's `j_lambdas` and the
two-lever factor schema. Compute `probs_90` from the goal model exactly as
v2 requires (strength shifts -> lambdas from the calibrated curve -> tempo
multiplier -> Dixon-Coles grid -> 1x2), using this competition's own fitted
curve as given to you in M, never a remembered constant from anywhere else.
