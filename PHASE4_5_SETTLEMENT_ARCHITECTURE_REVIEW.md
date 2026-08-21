# Phase 4.5 Architecture Review — Settlement Engine

Status: **approved. Implementation in progress on this branch.**

Branch: `feature/ai-earnings-forward-test`.

Scope of this document: designs the Settlement Engine that evaluates frozen, already-*entered*
AI Benchmark Portfolio decisions after the earnings event has passed. Per explicit instruction,
this phase touches only the `DecisionSnapshot -> EntrySnapshot -> SettlementSnapshot` chain — it
does **not** touch the frontend, track-record analytics, the performance dashboard, win-rate
calculation, or ML metrics. Those consume settlement data in a later phase; this phase only
produces it, honestly.

---

## Addendum (2026-08-21) — architecture decisions approved, superseding §6.2/§9 in part

The open questions in §9 were resolved by explicit direction. Where a decision below overrides
this document's own original recommendation, both are kept — the recommendation for its
reasoning, the decision as what's actually built — matching this project's existing convention
for a reviewed-and-reversed call (see `PHASE4_ARCHITECTURE_REVIEW.md`'s own §2.3 addendum).

1. **§9.1, historical reconstruction fallback — confirmed, as recommended.** Not built. Settlement
   is live-scheduled-capture only. Unavailable exit bid/ask → honest `FAILED` attempt; the
   historical-LAST-price path (`services/options_reconstruction.py`) is never called from this
   phase's code at all.

2. **§6.2, `return_pct` denominator — overridden.** Not `initial_max_risk` as this document
   recommended. Approved formula: `realized_pnl / initial_premium`, where `initial_premium` is
   the same signed, per-leg-aggregated quantity `EntryCaptureAttempt.net_entry_cash` already is
   (ASK × quantity × multiplier for a long leg, BID × quantity × multiplier, contributing as a
   credit, for a short leg — confirmed in §6.5/implementation to be *exactly*
   `analytics/options/payoff.py::OptionLeg.signed_premium`'s existing definition, aggregated the
   same way `compute_budget_fit` already aggregates it into `net_entry_cash`). Read via the new
   `entry_capture_attempt_id` FK (§6.2), never recomputed. Note kept from the original
   recommendation, not overridden by the decision: `net_entry_cash` can be negative for a
   net-credit strategy (`payoff.py`'s own documented sign convention) — `return_pct` is computed
   exactly as specified regardless, and is `None` only when the denominator is exactly zero
   (division by zero), never silently reinterpreted.

3. **§6.2, R-multiple — confirmed, as recommended.** `realized_pnl / initial_max_risk`, read via
   the same FK, never recomputed — "use the existing strategy risk calculation" is `EntryCapture
   Attempt.initial_max_risk`, computed once at entry by `compute_budget_fit` and never touched
   again.

4. **§6.2/§6.3, database design — overridden.** Not a widened `settlement_snapshot`. A new table
   pair, `settlement_capture_attempt` → `exit_snapshot`, is created instead, mirroring
   `entry_capture_attempt` → `entry_snapshot` column-for-column and mechanism-for-mechanism
   (same partial unique index shape, same `reject_snapshot_update()` trigger, same append-only-
   attempts semantics). **The existing `settlement_snapshot` table and `models/settlement_
   snapshot.py` are left completely untouched** — no `ALTER TABLE`, no new columns, no rows ever
   written to it by this phase's code, per explicit instruction ("Do not mutate SettlementSnapshot
   directly. SettlementSnapshot should remain immutable"). It remains in the schema as the
   original Phase 4.1 scaffold, now superseded and unused — the same way this document's §6.2
   observed `entry_snapshot`'s original flat-JSON Phase 4.1 design was superseded by Phase 4.4's
   real build, except here the old table is left in place rather than widened in place.
   `services/decision_lifecycle.py::has_settlement()` is repointed at `settlement_capture_attempt`
   (mirroring `has_official_entry()`'s existing query against `entry_capture_attempt` exactly,
   including the same `benchmark_portfolio_id` scoping it already has and `has_settlement` did
   not) — required for `SETTLED` to ever be derivable for a real Phase 4.5 settlement.

5. **§2, scheduler — confirmed, implemented as a separate job.** `run_exit_capture_job`, its own
   function and its own registered job id, at the same `15:55` `America/New_York` cron trigger
   `run_decision_and_entry_capture_job` already uses (§2's open question #2 resolved in favor of
   separation, since the two jobs scan disjoint decision sets — "just entered, nothing to exit
   yet" vs. "already entered, due for exit" — and a missed/slow entry run should never block or
   delay the unrelated exit run for a different day's decisions).

6. **§9.4, API — overridden (a read endpoint ships this phase after all), but narrower than
   proposed.** Not the `/decision-snapshots/{id}/settlements` shape this document sketched by
   analogy to the existing `/entries` endpoint. Exactly `GET /settlements/{decision_id}`, read-
   only, no mutation endpoint — mirrors `benchmark_entries.py`'s own docstring rationale
   (`api/routers/decision_snapshots.py`'s own docstring: not exposing a write path is a stronger
   guarantee than exposing one and trusting callers not to misuse it) applied to settlement.

7. **§9.5, `earnings_result` — moot.** It lived on `settlement_snapshot`, which this phase no
   longer writes to at all (decision 4). No action needed.

**Explicit scope confirmation carried forward unchanged from §8**: this phase implements exit
capture, settlement calculation, and immutable storage, plus their tests — never the Track Record
dashboard, win-rate aggregation, performance analytics, or any frontend work.

---

## 0. Pre-flight verification

Confirmed directly (`git branch`, `git log`), not assumed:

| Check | Result |
|---|---|
| Current branch | `feature/ai-earnings-forward-test` ✓ |
| Phase 4.1 (`c5c3456`) | Present — database foundation: `decision_snapshot`, `entry_snapshot`, `settlement_snapshot`, `benchmark_portfolio` tables created. |
| Phase 4.2 (`254e9a6`) | Present — Finnhub calendar sync, eligibility, scheduler skeleton, read API. |
| Phase 4.3 (`9d30e4c` review, `805517a` impl) | Present — decision freezing, full `decision_snapshot` immutability (including its own `status` column — see §6.1). |
| Phase 4.4 (`8286c84`) | Present — benchmark policy config, `EntryCaptureAttempt`/`EntrySnapshot`, official entry capture, scheduler wiring, read-only entry API. |
| Phase 4.4 hardening (`d1b9ae3`) | Present — live underlying context (`get_underlying_quote`), two-sided entry window (`EARLY_CAPTURE_TOLERANCE`/`LATE_CUTOFF_GRACE`). |
| Test cleanup (`a351820`) | Present — HEAD. Full suite green (1045 passed, 0 failed) as of this commit. |

Working tree clean (only the pre-existing, unrelated `.claude/` untracked directory). Nothing to
rebase — this branch has been the active line of work for the whole Phase 4 effort.

---

## 1. Historical option pricing availability

This is the question the whole design pivots on, so it was investigated first and concretely,
against real code and real prior findings — not assumed.

### 1.1 What each existing IBKR path actually provides

| Path | File | What it returns | Historical? |
|---|---|---|---|
| Live snapshot | `providers/ibkr_options.py::_fetch_snapshots` / `get_underlying_quote` (Phase 4.4 hardening) | Real bid/ask/last/Greeks for **right now**, via `/iserver/marketdata/snapshot` | No — live only. This is what entry capture uses. |
| Historical bars | `providers/ibkr_historical.py::fetch_historical_bars` | Real **trade/LAST-price OHLC bars**, 1-minute granularity, for a **completed** session, via `/iserver/marketdata/history` | Yes, but LAST-price only. |
| Reconstruction | `services/options_reconstruction.py` (Phase 14.13) | Combines the historical bars above into a "close-window" (15:55–16:00 ET) chain reconstruction, tagged `pricing_source="historical_last"` | Yes, for a just-completed session only, LAST-price only. |

`providers/ibkr_historical.py`'s own module docstring is explicit and was confirmed live (WMT,
2026-08-19) during Phase 14.13: *"The Client Portal Web API does **not** expose a documented
bid/ask/midpoint bar type (unlike the classic TWS API's `whatToShow=BID_ASK`) — every bar this
project reads from it is a TRADE/LAST price, never fabricated into a synthetic bid/ask."*

**This is the load-bearing fact for the whole exit-pricing design:** IBKR can genuinely tell us
*a* real historical price for a *just-completed* session's close window, but it can never tell us
what the **bid** or **ask** was at that moment — only what actually traded last. The Client
Portal Gateway's live snapshot endpoint is the *only* IBKR path that ever returns real, two-sided
bid/ask, and it only ever returns it for the current moment.

### 1.2 Can IBKR provide the required historical option chain?

**No, not the way the pricing rule (§3) requires.** The pricing rule demands BID for a long exit
and ASK for a short exit — a genuine two-sided market observation. IBKR's only source of
after-the-fact data (`/iserver/marketdata/history`) cannot supply that for options, confirmed
live and documented in `providers/ibkr_historical.py`'s own docstring. It can supply LAST price
for a completed session's close window, which is a different, lower-fidelity thing.

### 1.3 Is any provider that *can* do this available?

No. `docs/data_sources.md` (§ options providers) already surveys this exact gap and its own
conclusion, unchanged since it was written, is:

> *"ORATS / CBOE DataShop — historical options chains incl. IV — Paid, priced per dataset — Only
> realistic source of genuinely historical (not just current-snapshot) options data; deferred
> until justified by cost."*

`docs/limitations.md` independently confirms the same thing from the replay-engine side: *"No
historical options-chain provider is wired up."* Alpha Vantage's options endpoints are
current-snapshot only (same as IBKR's live path, just delayed/lower quality). Tradier is a listed
candidate but not implemented. Nothing in this codebase, today, can answer "what was the real
bid/ask for this contract three days ago."

### 1.4 The direct consequence for settlement's design

Since no provider can retroactively reconstruct a real bid/ask, **settlement must work exactly
the way entry capture works: as a scheduled job that runs live, at the scheduled exit moment
(T+1, 15:55 ET), fetching a real, current two-sided quote for the still-open position.** It is
not a "look up yesterday's price" operation — it is "capture the live market, on time, for the
contracts already on record." This is the single biggest design implication of this section, and
it shapes §2, §4, and §5 below.

A missed live window (service down at exactly 15:55 ET T+1, IBKR Gateway not authenticated, etc.)
is a real, recurring, already-observed risk — the same one Phase 4.4's own architecture review
flagged for entry capture (§4.1: *"IBKR Gateway not being authenticated (real, recurring
operational risk — confirmed live twice during earlier phases of this project)"*). For entry,
a missed window just produces an honest `FAILED` attempt. For exit, the §1.1 historical-bars path
offers a second option a missed entry never had: a real, honestly-labeled, LAST-price-only
reconstruction of the missed close. **Recommendation: do not let this count as an official
settlement.** This mirrors the exact precedent this project just set for entry capture (Phase
4.4 hardening, `d1b9ae3`): a previous-session daily close was available as a fallback for the
*underlying* price, and the explicit decision was to remove it from the official path rather than
let a lower-fidelity substitute silently pass as "captured." The same reasoning applies here, for
the same reason: BID-for-long/ASK-for-short is the frozen pricing contract (§3), and LAST-price
reconstruction structurally cannot honor it. If retained at all, it should be a clearly-labeled,
separate, non-official research value — never something that flips a decision's lifecycle to
`SETTLED`. **This is flagged as an explicit open question in the summary (§9.1) since it's a real
product trade-off (some settlement data, later, vs. none) and not purely a technical call.**

### 1.5 What existing snapshots/infrastructure can be reused

- **`EntrySnapshot.external_contract_id`** (Phase 4.4) — the exact IBKR conid for each leg,
  captured once at entry and never re-derived. Settlement should re-quote **these specific
  conids**, not re-run ATM/strike selection (`get_option_chain`'s own strike-discovery flow) —
  the underlying may have moved since entry, and re-selecting strikes could silently quote a
  *different* contract than the one actually held. `providers/ibkr_options.py::_fetch_snapshots`
  already takes `contracts: list[tuple[Decimal, str, int]]` (strike, right, conid) internally —
  the conid-driven snapshot mechanism already exists; what's missing is a small public entry
  point that skips discovery and takes already-known conids directly (see §6.4).
- **`get_underlying_quote(ticker)`** (Phase 4.4 hardening, `d1b9ae3`) — already the correct,
  live, contemporaneous underlying-context mechanism; reused as-is for the exit side's underlying
  price/timestamp, with the same `MAX_UNDERLYING_OPTION_SKEW` coherence check.
- **`OptionsProviderChain`** (`providers/fallback.py`) — already delegates both `get_option_chain`
  and `get_underlying_quote` correctly (hardened in `d1b9ae3`); no changes needed for settlement
  to use the same provider chain the scheduler already builds.
- **`EntryCaptureAttempt.initial_max_risk`** — the R-multiple unit is already computed and stored
  at entry time (Phase 4.4 sec 9, via `compute_budget_fit`). Settlement reads it via a new FK
  (§6.2), never recomputes it.
- **`compute_entry_exit_schedule()`** (`analytics/earnings_timing.py`) — already computes
  `exit_date`/`exit_timestamp` correctly for both BMO and AMC; see §2.

---

## 2. Exit rule

`analytics/earnings_timing.py::compute_entry_exit_schedule()` already computes this correctly —
confirmed by direct execution against the user's own two worked examples, not just by reading the
code:

```
AMC — earnings Monday 2026-09-14, AFTER_MARKET:
  decision_date = nearest_trading_day_on_or_before(2026-09-14) = 2026-09-14 (Monday itself)
  exit_date     = next_trading_day(2026-09-14)                = 2026-09-15 (Tuesday)
  exit_timestamp = 2026-09-15 15:55:00 America/New_York

BMO — earnings Tuesday 2026-09-15, BEFORE_MARKET:
  decision_date = previous_trading_day(2026-09-15)             = 2026-09-14 (Monday)
  exit_date     = nearest_trading_day_on_or_after(2026-09-15)  = 2026-09-15 (Tuesday itself)
  exit_timestamp = 2026-09-15 15:55:00 America/New_York
```

Both match the user's worked examples exactly. **No new timing logic is needed.** This is the
same engine `benchmark_entry_capture.py::_verify_no_lookahead` already imports and reuses for
entry (`compute_entry_exit_schedule(...).entry_timestamp`); settlement reuses the same call's
`.exit_timestamp`/`.exit_date` fields, never a second implementation of BMO/AMC/DMH/holiday
logic. `ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE` (`models/enums.py:321-328`) is the only
value that enum has ever had — its own docstring already says *"Phase 4.5 will be the first thing
that actually acts on it"* — confirming this is exactly the policy in scope, with no ambiguity
about which of multiple possible exit policies to implement.

**Scheduling implication:** a new scheduler job, symmetric with
`run_decision_and_entry_capture_job` (`services/scheduler.py`), registered at the same wall-clock
trigger (15:55 ET daily, `America/New_York`) — not a new time, since every eligible exit
resolves to the same `ENTRY_EXIT_TIME` the entry job already runs at (`analytics/earnings_
timing.py::ENTRY_EXIT_TIME = time(15, 55)`). The job scans for `DecisionSnapshot` rows that are
`ENTERED` (per `decision_lifecycle.py`, §6.1) whose `EntryCaptureAttempt`'s calendar event
resolves an `exit_timestamp` matching today — not "every event," the way the entry job scans
every `UPCOMING` calendar event. Whether this is the *same* job function (entry + exit checked in
one daily run, mirroring how `run_decision_and_entry_capture_job` already does decision-generation
+ entry-capture in one pass) or a second, separate job is an open implementation choice with no
strong reason to prefer one over the other — flagged in §9.2.

---

## 3. Pricing rules

Confirmed as the exact mirror image of the already-implemented, already-hardened entry rule
(`services/benchmark_entry_capture.py::_price_leg`):

| Leg type | Entry (existing, Phase 4.4) | Exit (new, Phase 4.5) |
|---|---|---|
| Long (`OptionAction.BUY`) | ASK (`BUY_TO_OPEN_AT_ASK`) | **BID** (`SELL_TO_CLOSE_AT_BID`) |
| Short (`OptionAction.SELL`) | BID (`SELL_TO_OPEN_AT_BID`) | **ASK** (`BUY_TO_CLOSE_AT_ASK`) |

Never the midpoint, never last — same discipline, same reasoning (§3 of Phase 4.4's own report),
extended to the closing side. `_price_leg` itself is not reused as-is (it names the *opening*
action), but the pattern (read `EntrySnapshot.action`, branch on BUY vs SELL, require the
opposite-side quote field, fail the leg honestly if that side is missing rather than fabricate
from `last`) is copied exactly — one new, small function, e.g. `_price_exit_leg`, not a
generalization that risks quietly changing the entry rule too.

**Quantity/multiplier are frozen at entry and never re-derived.** Settlement closes the *exact*
position that was opened — `EntrySnapshot.quantity`/`.multiplier` (already captured, immutable)
are copied forward, not recomputed via `compute_budget_fit` again. There is no new sizing
decision at exit; this significantly simplifies the settlement service relative to entry capture
(`analytics/decision/budget.py` is not needed at all for settlement — see §6.5 for the small,
new, pure P&L arithmetic that *is* needed).

**Sign convention** (derived once, applied uniformly, no per-leg-type branching needed in the
actual math): for a leg with `direction_sign = +1` (BUY) or `-1` (SELL),

```
realized_pnl_per_share = (exit_fill_price - entry_fill_price) * direction_sign
```

Verified against both cases: BUY (`+1`) → `bid_exit - ask_entry` (gain if it appreciated exactly
as the conservative entry/exit rule implies); SELL (`-1`) → `bid_entry - ask_exit` (gain if it
decayed, since the position was opened as a credit and closed by paying it back). This one
formula is the entire per-leg P&L calculation — no strategy-shape-specific logic needed, exactly
like `analytics/options/payoff.py`'s existing philosophy of deriving results generically rather
than hand-coding per strategy type.

---

## 4. No look-ahead bias

Three distinct mechanisms, all direct extensions of precedent already built and tested in this
project (nothing here is a new category of defense):

1. **Two-sided capture window**, mirroring Phase 4.4 hardening's `EARLY_CAPTURE_TOLERANCE` /
   `LATE_CUTOFF_GRACE` (`services/benchmark_entry_capture.py::_verify_no_lookahead`), applied to
   `schedule.exit_timestamp` instead of `schedule.entry_timestamp`. A capture materially before
   the scheduled T+1 15:55 ET moment must be rejected exactly as it is for entry (the earnings
   reaction may not have fully played out yet — capturing at, say, T+1 10:00 ET after a BMO
   release the same morning would still be too close to the event to represent a full session's
   reaction). A capture materially after must also be rejected — an exit price sourced days late
   is not "the exit," and (§1.4) IBKR cannot honestly reconstruct a two-sided market for a day
   that has already closed, so a late settlement attempt has no honest live path to fall back to
   at all; it must simply fail (§5).
2. **Never use the current/live option chain to stand in for a missed exit.** This is the exact
   failure mode Phase 4.4's hardening pass eliminated on the entry side (previous-session daily
   close silently substituted for live underlying context) — the settlement analog would be
   quoting *today's* live chain for a position whose real exit moment was days ago, which is
   simply the wrong day's market, not an approximation of the right one.
3. **Historical reconstruction, if used at all (§1.4, §9.1), must never satisfy the official
   settlement.** `EntrySnapshot.pricing_source`'s own docstring already states this exact
   principle for entry — *"Never 'reconstructed_*' for an official entry ... this field is where
   that would show up if it ever slipped through, so it's honestly recorded, not just prevented
   by convention"* — and the same field/convention extends naturally to a new `ExitSnapshot.
   pricing_source` (§6.3): a value like `historical_last` is honestly recordable, but must never
   be what makes a `SettlementSnapshot` row `CAPTURED`.

No new timing engine, no new coherence-checking primitive — `MAX_UNDERLYING_OPTION_SKEW`
(`services/options_reconstruction.py`, already reused by entry capture) is reused a third time
for the exit side's underlying/option timestamp coherence check.

---

## 5. Failure handling

Directly inherits the append-only-attempts discipline this project has used at every stage since
Phase 4.1 (§2.3 of `PHASE4_ARCHITECTURE_REVIEW.md`), already implemented and tested for entry:

- **Missing/incoherent exit data → honest `FAILED` `SettlementSnapshot` row**, real
  `capture_error`, never a fabricated price. Same shape as `EntryCaptureAttempt`'s `FAILED`
  outcome.
- **All-or-nothing across legs**, mirroring Phase 4.4 sec 13's rule that *"no partial multi-leg
  entry counts as a benchmark trade"* — if any leg's exit quote is missing the required side
  (BID for a long, ASK for a short), the whole settlement attempt fails; a 3-of-4-leg iron
  condor exit is not a real, honest closed position.
- **Never a partial settlement counted as final.** `decision_lifecycle.py::has_settlement` only
  needs to check for a `status=CAPTURED` row, exactly as it does for `has_official_entry` — a
  `FAILED` attempt leaves the decision `ENTERED`, not `SETTLED`, and (crucially, since this
  decision genuinely *did* enter a real position) the scheduler must retry on a later run rather
  than ever silently abandoning it.
- **Append-only retries.** A retried settlement attempt is a brand-new row, never an UPDATE —
  already structurally guaranteed by the existing `settlement_snapshot_no_update` trigger
  (§6.1), which predates this phase.
- **Provider exceptions** (IBKR Gateway not authenticated/unavailable — a real, previously
  observed failure mode per Phase 4's own architecture review) caught the same way
  `capture_benchmark_entry` already catches them: one `try/except` around both the option-chain
  and underlying-quote provider calls, producing a `FAILED` row with the real exception message,
  never a crash that takes the whole scheduler run down.

---

## 6. Database impact

### 6.1 Current state of `decision_snapshot` lifecycle — verified, one stale docstring flagged

`decision_snapshot.status` is a real column (`models/decision_snapshot.py:114-115`), but Phase
4.3 made the *entire* row immutable, including `status` — confirmed directly in that model's own
module docstring: *"a real Postgres BEFORE UPDATE trigger ... makes every column reject an
UPDATE, including `status`. This is a deliberate change from this table's original Phase 4.1
design (where `status` was meant to roll forward as entry/settlement capture happened)."*
`DecisionSnapshotStatus`'s own enum docstring (`models/enums.py:205-211`) still says *"Mutated
only by the entry/settlement capture jobs"* — that line is now stale (predates the Phase 4.3
reversal) and is not corrected by this review, since it's a documentation nit outside this
phase's stated scope, but is flagged here so it isn't mistaken for current behavior. **The real,
load-bearing mechanism is `services/decision_lifecycle.py`**, which derives `PENDING_ENTRY` /
`ENTERED` / `SETTLED` purely by querying for a `status=CAPTURED` row in `EntryCaptureAttempt` /
`SettlementSnapshot` — `decision_snapshot.status` itself is inert after insert. `has_settlement()`
already exists (`decision_lifecycle.py:56-65`) and already queries `SettlementSnapshot` — it was
written in Phase 4.3, ahead of Phase 4.5, specifically so this module wouldn't need a second
migration later. **This phase does not need to touch `decision_snapshot` or `decision_lifecycle.
py`'s `has_settlement` shape at all** beyond scoping it by `benchmark_portfolio_id` (see §6.2 —
`has_official_entry` already does this; `has_settlement` currently doesn't, a small, pre-existing
asymmetry this phase should close for consistency).

### 6.2 `settlement_snapshot` exists, but is stale relative to the *since-evolved* entry pattern

The table was created in Phase 4.1 (migration `24e13c12e7dc`) against the **original** §2.3 plan
in `PHASE4_ARCHITECTURE_REVIEW.md` — a single flat row per attempt with a JSON `leg_quotes`
blob for per-leg detail, explicitly deferring a normalized per-leg table (*"Not built now,
flagged for later ... V1's scale doesn't need it"*).

**Phase 4.4, when actually implemented, did not follow that plan for the entry side.** Instead of
a flat `entry_snapshot` row with JSON `leg_quotes`, it built exactly the normalized split the
original review deferred: `EntryCaptureAttempt` (the attempt) + `EntrySnapshot` (one real,
typed-column row per leg, FK'd via `capture_attempt_id`). This is a confirmed, real architectural
evolution beyond the document `settlement_snapshot` was originally designed against — not a
hypothetical. **Recommendation: `settlement_snapshot` should evolve to match the pattern that was
actually built, not the original, superseded plan it currently reflects.** Concretely:

- **Widen `settlement_snapshot` into the attempt-level row**, mirroring `EntryCaptureAttempt`
  column-for-column where the analogy holds:

  | Existing column | Disposition |
  |---|---|
  | `id`, `decision_id`, `status`, `capture_error`, `source_provider`, `created_at` | Keep as-is. |
  | `settled_at` | Keep — mirrors `EntryCaptureAttempt.captured_at` (when the row was written), distinct from the new `exit_market_timestamp` below (when the market data itself was observed) — the same distinction Phase 4.4 already draws between `captured_at` and `option_market_timestamp`. |
  | `earnings_date` | Keep (harmless, already-present denormalization; no migration action needed). |
  | `earnings_result` | Keep the column, **do not populate it in V1** — computing beat/miss/inline requires comparing actual EPS to a consensus estimate (a V3 `EarningsEstimateSnapshot`/`EarningsResult` concern), which is not part of this phase's stated scope (§ "exit timestamp, exit option prices, realized P&L, return %, R multiple, win/loss status"). Stays nullable, stays null. |
  | `price_before`, `price_after`, `realized_move_pct` | Keep and populate — `price_after` is naturally the underlying price observed at settlement (already being fetched via `get_underlying_quote` anyway, same as entry's `underlying_price`); `price_before` copies the entry's own `EntryCaptureAttempt.underlying_price` via the new FK below, no extra fetch needed. Useful context, not central to this phase's math, effectively free to populate. |
  | `option_exit_value` | **Rename** to `net_exit_price_per_share`, matching `EntryCaptureAttempt.net_entry_price_per_share`'s established naming. |
  | `realized_pnl` | Keep — matches the user's explicit ask. |

  New columns needed (mirroring `EntryCaptureAttempt`'s own shape):

  ```
  benchmark_portfolio_id   FK -> benchmark_portfolio.id, indexed        (mirrors EntryCaptureAttempt)
  entry_capture_attempt_id FK -> entry_capture_attempt.id, indexed      (NEW — which entry this closes;
                                                                          the only way to know what to
                                                                          diff exit prices against)
  underlying_bid                                                        (mirrors EntryCaptureAttempt;
  underlying_ask                                                        real bid/ask when the provider
                                                                          exposes them, never fabricated
                                                                          -- same rule as entry)
  underlying_timestamp
  exit_market_timestamp    (mirrors option_market_timestamp)
  net_exit_cash                                                         (mirrors net_entry_cash)
  return_pct               NEW, real typed column
  r_multiple                NEW, real typed column
  is_win                    NEW, boolean, real typed column
  ```

  `return_pct`/`r_multiple` should never be denominated by `net_entry_cash`/`net_exit_cash` —
  `analytics/decision/budget.py::compute_budget_fit` documents `net_premium` as signed (*"positive
  = net debit paid, negative = net credit received"*), so a net-credit strategy (e.g. an iron
  condor) would have a **negative** `net_entry_cash`, which would invert or blow up a percentage
  return computed against it. **Recommendation: denominate both by `EntryCaptureAttempt.
  initial_max_risk`** (always a positive magnitude — confirmed in `payoff.py`'s own docstring:
  *"max_loss: None = unbounded (positive magnitude otherwise)"*, and already used the same way by
  `compute_budget_fit`'s own `budget_utilization_pct`). This is also the textbook definition of
  "R" in R-multiple, and produces a stable, always-defined percentage for both debit and credit
  structures alike. Flagged as an explicit confirmation point in §9.3, since "return on capital
  deployed" is a defensible alternative reading of "return %" and this review is picking the one
  that's actually well-defined for every strategy shape this project already generates.

  A new partial unique index, mirroring `uq_entry_capture_attempt_one_captured_per_decision_
  portfolio` exactly: `uq_settlement_snapshot_one_captured_per_decision_portfolio` on
  `(decision_id, benchmark_portfolio_id) WHERE status = 'CAPTURED'` — the same DB-level "at most
  one operative settlement" guarantee entry already has, at most one query away.

### 6.3 New table: `exit_snapshot` (per-leg, mirrors `entry_snapshot` exactly)

Same reasoning as §6.2 — the per-leg grain Phase 4.4 already established for entries should be
symmetric on the exit side, not a regression to a JSON blob. This directly serves the user's
explicit "multi-leg strategy" testing requirement (§7): a 4-leg iron condor's realized P&L is
only real and auditable if each leg's own BID/ASK exit fill is a real, queryable row, exactly like
each leg's own entry fill already is.

```
id                        PK
decision_id                FK -> decision_snapshot.id, indexed (not unique — mirrors entry_snapshot)
settlement_attempt_id       FK -> settlement_snapshot.id, indexed
entry_snapshot_id           FK -> entry_snapshot.id, indexed (the exact entry leg this exit closes —
                                                                lets a reader compute per-leg P&L with
                                                                one join, never a second lookup)
leg_index                   int (0-indexed, matches entry_snapshot.leg_index for the same leg)
status                      CaptureStatus (reused enum, create_type=False)
captured_at                 datetime, indexed

-- contract identity, copied forward from the entry leg (self-contained, no-join audit row,
-- exactly like entry_snapshot's own philosophy)
external_contract_id, expiration, strike, option_type, action, quantity, multiplier

-- raw exit quote (never a computed fill price)
bid, ask, mid, last_price, implied_volatility, delta, gamma, theta, vega, market_data_quality
pricing_source               (never "reconstructed_*"/"historical_last" for an official exit — §4.3)

-- official benchmark close (mirrors entry_snapshot's benchmark_entry_price/pricing_assumption)
benchmark_exit_price         BID for a long leg, ASK for a short leg
pricing_assumption           e.g. "SELL_TO_CLOSE_AT_BID" / "BUY_TO_CLOSE_AT_ASK"
realized_pnl_per_share       leg-level P&L (§3's formula) -- lets a reader verify the attempt-level
                                                              realized_pnl by summing real rows,
                                                              never a number that must be trusted
                                                              without a per-leg audit trail

capture_error, source_provider, created_at

UniqueConstraint(settlement_attempt_id, leg_index)  -- mirrors uq_entry_snapshot_attempt_leg
```

Same `reject_snapshot_update()` trigger as every other Phase 4 snapshot table — the migration
that creates `exit_snapshot` installs it exactly the way `78ee400f83ab` (entry_snapshot) and
`24e13c12e7dc` (settlement_snapshot) already did; no new trigger *function* needed, only a new
`CREATE TRIGGER ... EXECUTE FUNCTION reject_snapshot_update()` referencing the already-existing
function.

### 6.4 Provider-layer change: quote by known conid, not by re-discovered strike

Small, additive, same shape as the Phase 4.4 hardening pass's `get_underlying_quote` addition —
not a redesign. `IBKROptionsProvider` needs one new public method (name TBD at implementation
time, e.g. `get_quotes_by_contract_id(conids: list[int], as_of: datetime) -> list[OptionQuote]`)
that calls the existing `_fetch_snapshots`-style priming/snapshot mechanism directly against
already-known conids, skipping `_resolve_underlying`/`_strikes_near_atm`/`_resolve_target_
expiration` entirely — those exist to *discover* which contracts to quote when only a ticker and
a target date are known (the entry-capture case); settlement always already knows exactly which
contracts it's closing (`EntrySnapshot.external_contract_id`), so re-running discovery is both
unnecessary and risks silently drifting to a different contract if the underlying moved since
entry. `OptionsProviderChain` gets the same delegation treatment as `get_option_chain`/`get_
underlying_quote` (try each provider in order, fall through on failure or "unsupported").

### 6.5 New pure math, no new options-math engine

`analytics/options/payoff.py::analyze()` and `analytics/decision/budget.py::compute_budget_fit()`
are **not** reused for settlement — they answer "what could this position look like" (a payoff
diagram, a sizing decision), and settlement doesn't need either: sizing is frozen at entry (§3),
and realized P&L is direct arithmetic on two known prices (§3's formula), not a payoff-diagram
extremum search. A small new pure module (e.g. `analytics/decision/settlement_math.py`) computing
per-leg and aggregate realized P&L, `return_pct`, `r_multiple`, and `is_win` from already-captured
`EntrySnapshot`/exit-quote data is the only new "math" this phase needs — deliberately not folded
into `payoff.py` (a different, options-payoff-diagram concern) or `budget.py` (a different,
pre-trade-sizing concern).

### 6.6 Migration count and risk

**A migration is required** — this is not a zero-schema-change phase like the 4.4 hardening pass
was. Concretely: widen `settlement_snapshot` (add ~10 columns + rename 1 + 2 new FKs + 1 partial
unique index) and create `exit_snapshot` (new table + trigger + indexes + unique constraint) —
two migrations, following this project's own established convention of one migration per logical
schema unit (e.g. Phase 4.4 split its combined diff into `641899980b94` → `b690e7dd35f0` →
`70d3fedfd3f4`). Known, previously-hit gotchas to avoid repeating (both already documented in this
project's own migration history and confirmed again during Phase 4.4):

- Reusing the existing `capture_status`/`option_type`/`option_action`/`market_data_quality` enum
  types requires `create_type=False` via a **separate `op.add_column()` step**, not inline inside
  `op.create_table()` — inline reuse doesn't reliably honor `create_type=False` (hit in Phase 4.1
  and again in Phase 4.4).
- No *new* enum types are needed here — every enum `exit_snapshot`/the widened `settlement_
  snapshot` need (`CaptureStatus`, `OptionType`, `OptionAction`, `MarketDataQuality`) already
  exists in the DB from prior Phase 4 migrations, so the "new enum type needs an explicit
  `.create(checkfirst=True)`" gotcha (also hit in Phase 4.1) does not apply this time — worth
  stating explicitly so it isn't reflexively re-applied where it isn't needed.
- `settlement_snapshot`'s existing `settlement_snapshot_no_update` trigger is unaffected by
  `ALTER TABLE ADD COLUMN` (a trigger only fires on `UPDATE`) — widening it is safe with the
  trigger left in place throughout.

**Immutability**: fully covered by existing infrastructure. `settlement_snapshot` has had
`reject_snapshot_update()` installed since Phase 4.1 (`24e13c12e7dc`) — confirmed by reading that
migration directly. The new `exit_snapshot` table gets the same trigger via one more `CREATE
TRIGGER` statement referencing the function that already exists. No new trigger *logic* is
required anywhere in this phase.

---

## 7. Testing strategy

Follows the exact discipline the Phase 4.4 hardening pass just established and validated (ruff +
mypy + full pytest, fixture-provider-driven, no live network) — same house style, extended to
the new module. Required minimum, directly from the user's list, plus the coherence/window
coverage precedent from Phase 4.4's own hardening tests:

**Core scenarios (explicitly requested):**
1. **AMC example** — earnings Monday AMC, entry Monday 15:55 ET, exit Tuesday 15:55 ET; assert
   the settlement job resolves the correct `exit_timestamp` via `compute_entry_exit_schedule`
   (not re-derived).
2. **BMO example** — earnings Tuesday BMO, entry Monday 15:55 ET, exit Tuesday 15:55 ET (same
   calendar day as the earnings date itself, unlike AMC) — the case most likely to be gotten
   wrong by a naive "always T+1 from entry" implementation, since here exit and earnings_date
   coincide.
3. **Missing option data** — exit quote unavailable for one or more legs (provider returns
   nothing, or only `last` with no bid/ask) → `FAILED` `SettlementSnapshot`, honest
   `capture_error`, decision stays `ENTERED` not `SETTLED`, no `exit_snapshot` row claims
   `CAPTURED`.
4. **Multi-leg strategy** — a real 3-leg (butterfly) and 4-leg (iron condor) case, mirroring
   `test_services_benchmark_entry_capture.py`'s own `_butterfly_legs()`/`_iron_condor_legs()`
   fixtures exactly (same contracts, now also given exit quotes) — assert every leg gets its own
   `exit_snapshot` row, and the attempt-level `realized_pnl` equals the real sum of each leg's
   `realized_pnl_per_share * quantity * multiplier`.
5. **Long option** — BID used at exit, never ASK/mid/last.
6. **Short option** — ASK used at exit, never BID/mid/last.

**Additional coverage, directly precedented by Phase 4.4's own hardening tests (same reasoning
applies here, not optional extras):**
7. Exit window: exact-target accepted, early-tolerance boundary accepted/rejected,
   late-tolerance boundary accepted/rejected — same 4-case shape as
   `test_capture_exactly_at_scheduled_entry_accepted` etc., now against `exit_timestamp`.
8. Underlying/option timestamp skew at exit — coherent accepted, excessive skew rejected
   (`MAX_UNDERLYING_OPTION_SKEW`, reused a third time).
9. Unavailable live underlying at exit → `FAILED`, never a stale/reconstructed substitute — the
   direct settlement-side analog of the entry-side test added in the hardening pass.
10. Historical-reconstruction data (if built at all per §9.1) can never accidentally satisfy an
    official settlement — same shape as the entry-side "stale daily close never satisfies
    official capture" test.
11. Idempotency: a second settlement call for an already-`CAPTURED` decision returns the existing
    row, no duplicate; a `FAILED` attempt allows a real retry (new row, not an update) — mirrors
    `test_successful_duplicate_capture_does_not_duplicate_benchmark_entry` / `test_failed_
    attempt_allows_a_new_retry_attempt`.
12. Immutability: attempting to mutate a `CAPTURED` `settlement_snapshot`/`exit_snapshot` row
    raises via the DB trigger — mirrors `test_failed_attempt_remains_immutable`.
13. Partial-leg failure → whole attempt `FAILED`, never a partial settlement (§5).
14. R-multiple/return-% math, table-driven: a losing trade, a breakeven-ish trade, and a
    capped-max-loss trade, each independently hand-computed and cross-checked — same practice
    already used for this project's Wilson-CI probability tests and flagged in the original
    Phase 4 review's own testing section.
15. Scheduler wiring: the new exit job is registered under a fixed id with `replace_existing=
    True` (no duplicate jobs across a restart) — mirrors `test_services_scheduler.py`'s existing
    coverage of the entry job.

Explicitly **not** in scope for this phase's tests (per the stated restrictions): win-rate
aggregation across multiple settlements, track-record/expectancy/profit-factor/drawdown
analytics, any frontend rendering, any ML feature-extraction. Those consume `SettlementSnapshot`/
`ExitSnapshot` rows written by this phase, in a later phase.

---

## 8. Explicit scope boundary (restated, not just implied)

Per the user's own instruction, this phase must not modify:
- **Frontend** — no new pages, routes, or components. `PHASE4_ARCHITECTURE_REVIEW.md` §7 already
  describes a `BenchmarkPortfolio.tsx` page consuming settlement data, but building it is a later
  phase's work, not this one's.
- **Track record analytics** — `services/track_record.py` (the *existing*, V3, `AIDecisionVersion`-
  based directional-accuracy engine) is untouched; so is any *new* Phase-4-flavored equivalent —
  building "Win Rate / Average R / Expectancy / Profit Factor / Max Drawdown" (§8 of the original
  Phase 4 review) is explicitly a later phase's work.
- **Performance dashboard** — no new aggregate read endpoint beyond what's needed to inspect one
  decision's own settlement (see §9.4 — even that is flagged as an open question, not assumed).
- **Win rate calculation** — `is_win` is stored per-settlement (a fact about *that* decision),
  but no cross-decision aggregation (win rate = wins/total across many decisions) is computed or
  exposed anywhere in this phase.
- **ML metrics** — no feature-extraction job, no evaluation harness. The original Phase 4 review's
  stated reason for real typed columns (§2.3: *"a feature-extraction job querying 'every settled
  decision's R-multiple' should never have to parse JSON to do it"*) is honored by this phase's
  schema design (§6.2/6.3), but building that job is explicitly out of scope here.

---

## 9. Summary — open questions requiring confirmation before coding

1. **Historical reconstruction as a non-official fallback (§1.4, §4.3).** Recommendation: build
   settlement as live-only in V1 (a missed window is an honest `FAILED`, exactly like a missed
   entry), and do **not** build the historical-LAST-price reconstruction fallback at all in this
   phase — it adds real complexity (a second data path, a second `pricing_source` value, a second
   set of "never let this count as official" tests) for a fallback whose own data quality can't
   satisfy the pricing rule anyway. Confirm this scope cut, or specify that the reconstruction
   fallback should be built now as a clearly-labeled, non-`CAPTURED`, research-only value.
2. **One combined entry+exit scheduler job, or two separate jobs (§2).** `run_decision_and_entry_
   capture_job` already does decision-generation + entry-capture in one daily pass; exit capture
   could either join that same function (one job scans both "what needs entering" and "what needs
   exiting" each run) or be a second, independent job function under the same 15:55 ET trigger.
   No strong technical reason favors one over the other — a scope/readability call.
3. **`return_pct`/`r_multiple` denominator (§6.2).** Recommendation: `initial_max_risk` (always
   positive, well-defined for both debit and credit structures) rather than `net_entry_cash`
   (signed, can be negative for a credit structure, per `payoff.py`'s own documented convention).
   Confirm, or specify a different intended denominator.
4. **Any read endpoint at all this phase, or purely internal (§8).** The literal scope given —
   `DecisionSnapshot -> EntrySnapshot -> SettlementSnapshot`, engine only — reads as excluding API
   work, unlike Phase 4.4 which shipped its capture service *and* a read-only entries endpoint in
   the same phase. Recommendation: **no new endpoint in Phase 4.5** — `SettlementSnapshot`/
   `ExitSnapshot` rows are real and queryable via direct DB access (or a future phase's endpoint)
   the same way `entry_capture_attempt`/`entry_snapshot` briefly were before Phase 4.4 added
   `GET /decision-snapshots/{id}/entries`. Confirm this reading, or specify that a symmetric
   `GET /decision-snapshots/{id}/settlements`-style read endpoint should ship in this phase too.
5. **`earnings_result` (beat/miss/inline).** Recommendation (§6.2): leave the existing column in
   place, unpopulated, in this phase — it requires wiring to consensus-estimate data this phase
   doesn't otherwise touch, and isn't part of the user's explicit six-item settlement math list.
   Confirm, or specify it should be computed now.

Do not start coding until these are resolved.
