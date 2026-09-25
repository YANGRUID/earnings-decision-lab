# V4.2 Phase 2 — Independent Decision Engine

**Not production, not active.** V4.1 remains the control and the official recommendation path.
Phase 2 is gated behind its own flag (`V4_2_INDEPENDENT_SEARCH_ENABLED`, default `false`) and its
own prospective activation instant, and with the flag off it writes nothing and issues no
market-data request.

Phase 1 evidence is untouched and keeps its own identity. The two phases are **not one series**.

## Why a second phase rather than an edit

Phase 1's challenger answers a narrower question than its name suggests. It reads the control's
own frozen candidate rows, so its real question is:

> *of the candidates V4.1 constructed, on the single expiry V4.1 chose, is any one worth taking?*

A `NO_ACTION` under Phase 1 therefore means "V4.1's shortlist was unattractive", which is not the
same claim as "no trade was worth taking". Phase 2 asks the wider question: it builds its own
bounded multi-expiry candidate universe and lets each of the six configurations choose within it.

That changes the **search space** and the **selection unit**. It changes no threshold — the
absolute economic gate, the move-edge rule and the friction model are imported from Phase 1
unchanged, deliberately, so that any difference in outcome is attributable to what was *searched*
rather than to what was *tolerated*.

## The measured defects this responds to

All from the production database, none inferred from realized outcomes.

1. **Every forward event searched exactly one expiry.** All 15 challenger decisions carry
   `expiry_ladder_position = 0` on every candidate and one distinct expiration. The multi-expiry
   builder has existed and been tested since 2026-09-05; nothing in the decision path called it.

2. **The universe was frequently tiny.** AZO searched a universe of **one** candidate (V4.1
   constructed 15 and kept 1); CASY 5; CTAS 8. Mean 14.5 candidates on one expiry.

3. **The control's $2,000 capital screen deleted the structures that distinguish the
   configurations.** V4.1 classifies a candidate `CAPITAL_INCOMPATIBLE` when its entry cash
   exceeds the `PER_DECISION_CAPITAL` of $2,000, and Phase 1 reads only `RANKABLE` rows. Thirty
   candidates across eight events were removed this way, entry cash up to $29,750 — invisible to
   the three configurations that hold $10,000.

4. **The six configurations were arithmetically forced to agree.** Across all 90 production
   configuration rows, `CAPITAL_INCOMPATIBLE` and `RISK_CAP_EXCEEDED` bound **zero** times, and
   the six reasons per event are identical except for the configuration's own name. The
   per-configuration check asks only two questions — entry cash above the capital base, max loss
   above the risk cap — and `choose_v4_2_candidate_for_configuration` is never passed the max
   loss, so only the first can fire; and it cannot fire either, because defect 3 has already
   removed everything above $2,000. On both ACTION events all six configurations selected the
   same candidate.

5. **A consequence in Phase 1's own persisted evidence** (reported, deliberately not corrected —
   see "What was NOT changed"). Because the risk cap never binds, `size_configuration_position`'s
   one-contract floor produced positions above their own cap:

   | Event | Configuration | Held | Cap |
   |---|---|---:|---:|
   | DRI | $2,000 Conservative | $850 | $300 |
   | DRI | $2,000 Moderate | $850 | $600 |
   | PAYX | $2,000 Conservative | $930 | $300 |
   | PAYX | $2,000 Moderate | $930 | $600 |

   **Fixed 2026-09-24**, after this document was first written. Phase 1 now
   receives each candidate's max loss, so the cap binds at selection and a
   candidate reaching sizing always fits at one contract. The break is versioned:
   rows written under the fixed policy carry `v4.2-shared-candidate-v2`, the 15
   rows taken before it keep NULL, and neither is restamped. See
   "What was NOT changed" below for what that leaves alone.

## Architecture

```
ONE EVENT
   |
   +-- ONE common point-in-time evidence freeze
   |      DecisionView (the control's, already frozen -- no LLM call)
   |      underlying observation (one quote)
   |      historical move distribution (V4.2's own, point-in-time safe)
   |      listed option metadata (one security-definition call)
   |
   +-- ONE broad multi-expiry option universe
   |      bounded ladder, at most 3 rungs
   |      each rung: its OWN listed strikes, its OWN ATM straddle,
   |                 its OWN implied move, its OWN geometry
   |      contracts deduplicated across expiries, strategies and geometries
   |      each unique contract quoted exactly ONCE
   |
   +-- SHARED deterministic valuations
   |      every candidate priced once, at the same T+1 15:30 objective
   |      the absolute gate evaluated once per candidate
   |
   +-- SIX INDEPENDENT configuration decisions
          family permission | liquidity floor | capital base | risk cap
          then ranked, per configuration, over what survives
```

### Version strings

| Component | Value |
|---|---|
| Methodology | `v4.2-independent-search-v1` |
| Phase 1, cap never bound (**NULL in the table means this**) | `v4.2-shared-candidate-v1` |
| Phase 1, risk cap binding | `v4.2-shared-candidate-v2` |
| Candidate universe | `v4_2_independent_universe_v1` |
| Expiry ladder | `v4_2_expiry_ladder_v1` *(unchanged from Phase 1)* |
| Per-configuration ranking | `v4_2_per_configuration_ranking_v1` |
| Configurations | `v4-forward-configurations-v1` *(unchanged)* |
| Viability gate | `v4_2_viability_gate_v1` *(unchanged)* |
| Move edge | `v4_2_move_edge_v1` *(unchanged)* |
| Friction | `earnings_friction_v2` *(unchanged)* |
| Strategy registry | `v4-strategy-semantics-v2` *(unchanged)* |
| Timing policy | `v4-1530-entry-1530-t1-settlement-confirmed-timing-v3` *(unchanged)* |

### The stage order, and why it is this one

Every rejected candidate is counted exactly once, at the first stage that refuses it, so the
per-configuration census sums to the universe.

```
data honesty -> family -> capital -> risk cap -> liquidity -> economics -> move edge -> rank
```

The configuration's **own** rules run before the shared economic gate. If the absolute gate went
first, every configuration would report the same economics summary and the diagnostics would be as
indistinguishable as Phase 1's were — an operator could not tell a $300 cap from a $5,000 one.

### The capital screen, deliberately not inherited

`CAPITAL_INCOMPATIBLE` is the one V4.1 validity verdict Phase 2 does **not** treat as a
universe-level exclusion: it is relative to a $2,000 standardized capital and three configurations
hold $10,000. Every other validity refusal — missing required side, missing entry IV, unbuildable
scenario grid — remains a hard exclusion, because those are statements about whether a candidate
can be valued honestly at all.

## Request budget

Measured on deterministic fixtures, per event, by ladder depth:

| Rungs | Candidates | Unique contracts | Provider requests |
|---:|---:|---:|---:|
| 1 | 11 | 6 | 4 |
| 2 | 22 | 12 | 6 |
| 3 | 38 | 19 | 8 |

Three rungs is 3.5× the candidate universe for 2× the requests, because 46 leg references collapse
to 19 subscriptions before anything is quoted. Six configurations cost **one** acquisition, not
six. Growth across events is linear and independent: eight events cost exactly eight times one.

The forward-window deadline the control decision phase already obeys now reaches Phase 2 as well.
It stops events being **started**; an event in flight is always finished.

## Persistence

Phase 2 writes into Phase 1's tables, discriminated by `methodology_version`. Two sets of tables
would mean two entry paths, two settlement paths and two track records, and the first fix to one
would leave the other behind.

- **NULL means Phase 1**, permanently, for rows past and future. Existing rows are *not*
  backfilled: stamping a claim into frozen evidence that the evidence never made is what a forward
  test must never do.
- The idempotency constraint indexes
  `COALESCE(methodology_version, 'v4.2-shared-candidate-v1')` alongside the event, gate version and
  observed instant. COALESCE rather than a plain fourth column, because Postgres treats NULLs in a
  unique index as distinct and a plain column would let two Phase-1 rows for one window both be
  accepted.
- The event row's `selected_candidate_id` is **NULL by design**. A Phase-2 event has six
  decisions, not one; the six configuration rows are the decision.

## Entry and settlement

Unchanged methodology, extended reach. Long legs pay **ASK**, short legs receive **BID**, no
midpoint and no last price. Settlement resolves against the **frozen conIds** of the entry and can
never re-select a strike. The released fallback chain (executable → same-session EOD close →
expiration handling) is the same module.

What is new: Phase 2 selects structures the control never constructed, so its candidates usually
have no `V4ShadowCandidateLeg` to read. The entry path now falls back to the challenger's own
frozen `legs_json`, which carries the identical observation from the same acquisition — and only
where the control genuinely has nothing, so Phase 1 reads exactly what it always has. Contract
reuse is **counted**, not assumed: claiming total reuse would understate what an independent
search costs.

## Replay

Mostly a refusal, deliberately. Replaying a multi-expiry search on a past event needs that event's
listed metadata and per-expiry quotes; the `v4_chain_metadata_snapshot` table holds zero rows and
all 21 control decisions froze one expiry. Rebuilding from today's chain would measure hindsight,
so it is not offered — not behind a flag, not as a diagnostic.

| Mode | Events |
|---|---:|
| `FULL_REPLAY_SUPPORTED` | 0 |
| `PARTIAL_REPLAY_SUPPORTED` | 21 |
| `CANNOT_REPLAY_HONESTLY` | 0 |

What *is* supported is re-running the six configuration policies over each event's frozen
single-expiry universe, which isolates how much of the change comes from the configuration policy
alone. On the two events that traded, the configuration policy by itself splits the six across two
structures and declines the smallest configuration outright — where Phase 1 put all six into one.
No settlement, entry or realized figure is read anywhere in that path.

## What was NOT changed

- V4.1 methodology, ranking, thresholds, geometry, timing — untouched. Its
  `historical_sample_n = 0` behaviour remains part of the frozen control.
- V4.2 Phase 1 evidence — untouched, not relabelled, not backfilled.
- Every released threshold: economic viability, move edge, liquidity, friction. Phase 2 changes
  the search space and the selection unit, not what is tolerated.
- The four over-cap entry rows from defect 5. They are frozen forward evidence and keep saying
  what they said; the defect is recorded and versioned, not corrected in history. The **code** was
  fixed the same day, under `v4.2-shared-candidate-v2`, so the boundary is in the data.
- No naked shorts: the strategy registry contains no uncovered-short family, Phase 2 adds none,
  and a structure with no bounded maximum loss is never sized.
- No brokerage order, no order API.

## Activation

Two independent conditions, both required, and neither is sufficient alone:

1. `V4_2_INDEPENDENT_SEARCH_ENABLED=true`
2. `V4_2_INDEPENDENT_SEARCH_ACTIVATION_AT` set to an instant, and the event's **legal decision
   window** at or after it.

The boundary is checked against the legal decision window rather than the clock, so a late or
retried run cannot admit an event whose window opened before activation. It is enforced per event
and **counted** — an event excluded by a `WHERE` clause is invisible, and a boundary that silently
drops work is one nobody can audit.

Activation is gated on a **market-hours** live dry run, which has not yet been possible.
