# Lessons

Maintained by the weekly review only. Confirmed lessons may only add filters or
reduce size; they can never loosen limits. Maximum 15 lessons in total.

Format:

```
### L-001: <statement>
- Effect: filter | reduce_size (<factor>) | info
- Evidence: n=<trades>, win rate, avg R, span <dates>
- Added: <date> · Last reviewed: <date>
```

## Confirmed

Lessons with ≥10 trades spanning ≥2 calendar weeks and a consistent effect. These are binding (tighten only).
A `reduce_size` lesson must state its factor (0 < factor ≤ 1), e.g. `- Effect: reduce_size (0.5)`;
the trade run passes it as `enter --size-factor 0.5`.

## Tentative

Observations with some supporting evidence. Information only; they never block a trade.

## Retired

Lessons that were withdrawn (e.g. skipped trades would have outperformed, or the evidence weakened). Kept for history.
