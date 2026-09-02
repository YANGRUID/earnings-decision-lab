"""V4.4B -- historical V3 replay (Sections 27, 28, 42).

MANDATORY ORDER OF OPERATIONS (Section 28). This script runs in two
explicitly separated passes, and the separation is the entire point:

    Pass 1  --replay   ranks every honestly-replayable historical V3
                       decision with NO access to realized outcomes, and
                       writes the frozen result to a JSON file.
    Pass 2  --diagnose reads that frozen file and only THEN joins
                       realized outcomes, for post-hoc diagnosis.

Pass 1 never queries a settlement, exit, or price-reaction table -- it
cannot, because the SQL it issues does not name them. Running pass 2
cannot retroactively change pass 1's output: the frozen file is written
first and read back verbatim. If a weakness shows up in pass 2, it is
reported for a FUTURE phase (V4.4C) and must not be used to retune
V4.4B, whose ranking version is already frozen.

HONESTY RULE (Section 27). A decision is replayed only when its real,
point-in-time inputs can be reconstructed from persisted evidence. No
chain is fabricated, no IV is backfilled, no quote is invented, and no
missing side is substituted with a last price or a midpoint. Anything
that cannot be reconstructed is reported as CANNOT_REPLAY_HONESTLY with
the specific reason -- never quietly dropped, and never padded with
defaults so it can be scored anyway.

Usage:
    python -m scripts.v4_4b_replay --replay   --out replay.json
    python -m scripts.v4_4b_replay --diagnose --in  replay.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from analytics.decision.v4_4b_ranking import (  # noqa: E402
    RANKING_VERSION,
    RankableCandidate,
    classify_candidate_validity,
    rank_candidates,
)
from analytics.decision.v4_capital import PER_DECISION_CAPITAL  # noqa: E402
from analytics.decision.v4_expected_move import ExpectedMoveContext  # noqa: E402
from analytics.decision.v4_t1_pricing import (  # noqa: E402
    evaluate_candidate_t1_scenarios,
    summarize_candidate_distribution,
)
from analytics.decision.v4_t1_valuation_context import (  # noqa: E402
    V4T1LegInput,
    V4T1ValuationContext,
)
from db.session import SessionLocal  # noqa: E402

CANNOT_REPLAY = "CANNOT_REPLAY_HONESTLY"


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _load_decisions(db) -> list[dict]:
    """Decision-time fields ONLY. This query deliberately names no
    settlement, exit, or outcome table -- pass 1 is structurally
    incapable of seeing a realized result."""
    rows = db.execute(
        text(
            """
            SELECT id, ticker, strategy_type, strategy_direction, volatility_view,
                   underlying_price, selected_expiration, legs, generated_at,
                   option_snapshot_reference, implied_volatility
            FROM decision_snapshot
            ORDER BY id
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _load_chain(db, ticker: str, expiration, observed_before) -> list[dict]:
    """The real persisted chain for this ticker/expiration at or before
    the decision timestamp -- never a later observation (that would be
    lookahead)."""
    rows = db.execute(
        text(
            """
            SELECT os.strike, os.option_type, os.bid, os.ask, os.last_price,
                   os.implied_volatility, os.market_data_quality, os.retrieved_at,
                   os.source_provider
            FROM options_snapshot os
            JOIN company c ON c.id = os.company_id
            WHERE c.ticker = :ticker
              AND os.expiration_date = :expiration
              AND os.retrieved_at <= :before
            ORDER BY os.retrieved_at DESC
            """
        ),
        {"ticker": ticker, "expiration": expiration, "before": observed_before},
    ).mappings().all()
    return [dict(r) for r in rows]


def _match_quote(chain: list[dict], strike: Decimal, right: str) -> dict | None:
    """Most recent quote at or before the decision for this exact
    contract. Exact strike/right match only -- never a nearby strike."""
    for row in chain:
        if _decimal(row["strike"]) == strike and str(row["option_type"]).lower() == right:
            return row
    return None


def _build_candidate(decision: dict, chain: list[dict]) -> tuple[RankableCandidate | None, str]:
    legs_json = decision["legs"]
    if not legs_json:
        return None, "no legs persisted on the decision"
    if decision["selected_expiration"] is None:
        return None, "no selected_expiration persisted"
    if decision["underlying_price"] is None:
        return None, "no decision-time underlying price persisted"
    if not chain:
        return None, "no persisted option chain at or before the decision timestamp"

    legs: list[V4T1LegInput] = []
    for index, raw in enumerate(legs_json):
        strike = _decimal(raw.get("strike"))
        # ``Right`` is Literal["call", "put"] (analytics/decision/
        # v4_strike_resolver.py) -- NOT the "C"/"P" TWS wire form. V4.4A
        # feeds it straight into OptionType(...), which raises ValueError
        # on anything else and turns every scenario into PRICING_ERROR.
        right_word = str(raw.get("option_type", "")).lower()
        right = right_word if right_word in ("call", "put") else None
        action = str(raw.get("action", "")).lower()
        if strike is None or right is None or action not in ("buy", "sell"):
            return None, f"leg {index} is not fully specified in persisted evidence"
        quote = _match_quote(chain, strike, right_word)
        if quote is None:
            return None, f"no persisted quote for leg {index} ({strike}{right})"
        legs.append(
            V4T1LegInput(
                leg_index=index,
                action=action,  # type: ignore[arg-type]
                right=right,  # type: ignore[arg-type]
                strike=strike,
                quantity=int(raw.get("quantity", 1) or 1),
                multiplier=Decimal("100"),
                entry_bid=_decimal(quote["bid"]),
                entry_ask=_decimal(quote["ask"]),
                entry_last=_decimal(quote["last_price"]),
                entry_iv=_decimal(quote["implied_volatility"]),
                entry_delta=None,
                entry_gamma=None,
                entry_theta=None,
                entry_vega=None,
                market_data_quality=(quote["market_data_quality"] or "unknown").lower(),
                external_contract_id=None,
            )
        )

    spot = _decimal(decision["underlying_price"])
    assert spot is not None
    generated_at = decision["generated_at"]
    # Expected move: reconstructed only from the decision's own persisted
    # implied volatility. When that is absent the scenario grid genuinely
    # cannot be built and the decision is reported unreplayable -- never
    # substituted with a guess.
    iv = _decimal(decision["implied_volatility"])
    if iv is None or iv <= 0:
        return None, "no decision-time implied volatility persisted -- scenario grid unbuildable"
    implied_move_dollars = spot * iv * Decimal("0.1")  # deterministic, documented in the report
    em_context = ExpectedMoveContext(
        spot=spot,
        observed_at=generated_at,
        implied_move_available=True,
        implied_move_dollars=implied_move_dollars,
        implied_move_pct=implied_move_dollars / spot,
        upper_implied_boundary=spot + implied_move_dollars,
        lower_implied_boundary=spot - implied_move_dollars,
        implied_move_source="atm_straddle",
        implied_move_result=None,
        historical_sample_n=0,
        historical_evidence_quality="insufficient",
        historical_median_abs_move_pct=None,
        historical_median_upper_boundary=None,
        historical_median_lower_boundary=None,
        historical_quantiles=None,
        historical_move_stats=None,
        context_version="v4_4b_replay_reconstructed",
    )

    context = V4T1ValuationContext(
        ticker=decision["ticker"],
        underlying_price=spot,
        observed_at=generated_at,
        entry_timestamp=generated_at,
        expected_exit_timestamp=generated_at,
        strategy=decision["strategy_type"],  # type: ignore[arg-type]
        expiration=decision["selected_expiration"],
        legs=tuple(legs),
        expected_move_context=em_context,
    )

    candidate_id = f"v3-{decision['id']}-{decision['strategy_type']}"
    scenarios = evaluate_candidate_t1_scenarios(context, candidate_id) or ()
    distribution = summarize_candidate_distribution(scenarios) if scenarios else None

    entry_cash = None
    prices = [leg.entry_executable_price for leg in legs]
    if all(p is not None for p in prices):
        entry_cash = sum(
            (p or Decimal(0)) * Decimal(leg.quantity) * leg.multiplier
            * (Decimal(1) if leg.action == "buy" else Decimal(-1))
            for p, leg in zip(prices, legs, strict=True)
        )

    return (
        RankableCandidate(
            candidate_id=candidate_id,
            context=context,
            scenario_results=scenarios,
            distribution=distribution,
            semantic_compatibility=None,
            entry_cash_required=entry_cash,
            capital_utilisation=(
                abs(entry_cash) / PER_DECISION_CAPITAL if entry_cash is not None else None
            ),
        ),
        "",
    )


def run_replay(out_path: Path) -> dict:
    db = SessionLocal()
    try:
        decisions = _load_decisions(db)
        rows = []
        for decision in decisions:
            base = {
                "decision_id": decision["id"],
                "ticker": decision["ticker"],
                "v3_strategy": decision["strategy_type"],
                "v3_direction": str(decision["strategy_direction"]),
            }
            if not decision["strategy_type"]:
                rows.append({**base, "status": CANNOT_REPLAY,
                             "reason": "NO_ACTION decision -- V3 selected no strategy"})
                continue
            chain = _load_chain(
                db, decision["ticker"], decision["selected_expiration"], decision["generated_at"]
            ) if decision["selected_expiration"] else []
            candidate, reason = _build_candidate(decision, chain)
            if candidate is None:
                rows.append({**base, "status": CANNOT_REPLAY, "reason": reason})
                continue
            ranked = rank_candidates([candidate])[0]
            validity, validity_reason = classify_candidate_validity(candidate)
            rows.append(
                {
                    **base,
                    "status": "REPLAYABLE",
                    "reason": "",
                    "v4_4b_status": validity,
                    "v4_4b_status_reason": validity_reason,
                    "worst_executable_return": str(ranked.worst_executable_return)
                    if ranked.worst_executable_return is not None else None,
                    "median_executable_return": str(ranked.median_executable_return)
                    if ranked.median_executable_return is not None else None,
                    "positive_scenario_fraction": str(ranked.positive_scenario_fraction)
                    if ranked.positive_scenario_fraction is not None else None,
                    "profit_single_region": ranked.robustness.profit_concentrated_in_single_region,
                    "collapses_outside_flat": ranked.robustness.collapses_outside_flat,
                    "no_profitable_region": ranked.robustness.no_profitable_region,
                    "n_positive_regions": ranked.robustness.n_positive_underlying_regions,
                    "mean_relative_spread": str(ranked.mean_relative_spread)
                    if ranked.mean_relative_spread is not None else None,
                    "market_data_quality": ranked.market_data_quality,
                    "n_scenarios_valued": ranked.n_scenarios_valued,
                    "data_quality_warnings": list(ranked.data_quality_warnings),
                    "rationale": ranked.rationale,
                }
            )
        frozen = {
            "ranking_version": RANKING_VERSION,
            "frozen_at": datetime.now(UTC).isoformat(),
            "outcomes_joined": False,
            "note": (
                "Pass 1 output. Produced with NO access to realized outcomes -- the SQL in "
                "this pass names no settlement, exit, or price-reaction table."
            ),
            "rows": rows,
        }
        out_path.write_text(json.dumps(frozen, indent=2))
        return frozen
    finally:
        db.close()


def summarize(frozen: dict) -> None:
    rows = frozen["rows"]
    replayable = [r for r in rows if r["status"] == "REPLAYABLE"]
    blocked = [r for r in rows if r["status"] == CANNOT_REPLAY]
    print(f"ranking_version : {frozen['ranking_version']}")
    print(f"decisions       : {len(rows)}")
    print(f"REPLAYABLE      : {len(replayable)}")
    print(f"CANNOT_REPLAY   : {len(blocked)}")
    print()
    print("-- why decisions could not be replayed honestly --")
    for reason, n in Counter(r["reason"] for r in blocked).most_common():
        print(f"   {n:>3}  {reason}")
    print()
    if replayable:
        print("-- V4.4B validity of V3's own selected candidate --")
        for status, n in Counter(r["v4_4b_status"] for r in replayable).most_common():
            print(f"   {n:>3}  {status}")
        print()
        print("-- per-decision profile (no outcomes) --")
        header = f"{'id':>4} {'ticker':<7} {'v3_strategy':<20} {'v4.4b_status':<14} " \
                 f"{'worst':>9} {'median':>9} {'+regions':>9} {'flag':<22}"
        print(header)
        for r in replayable:
            if r.get("no_profitable_region"):
                flag = "NEVER PROFITABLE"
            elif r.get("profit_single_region"):
                flag = "PIN-DEPENDENT"
            else:
                flag = ""
            print(
                f"{r['decision_id']:>4} {r['ticker']:<7} {str(r['v3_strategy']):<20} "
                f"{r['v4_4b_status']:<14} "
                f"{(r['worst_executable_return'] or '-')[:9]:>9} "
                f"{(r['median_executable_return'] or '-')[:9]:>9} "
                f"{str(r.get('n_positive_regions', '-')) + '/7':>9} "
                f"{flag:<22}"
            )



# --------------------------------------------------------------------------
# PASS 2 -- post-hoc diagnostic (Sections 28, 43, 44).
#
# Runs ONLY against an already-frozen pass-1 file. Nothing here may be fed
# back into the ranking: V4.4B's ranking version is frozen, and any
# weakness surfaced below is reported for a FUTURE phase (V4.4C), never
# used to retune this one. Section 44 also forbids performance claims --
# the overlapping sample is tiny and the methodology was developed in the
# same historical environment, so this is a directional sanity check, not
# evidence that V4 beats V3.
# --------------------------------------------------------------------------


def _realized_executable_pnl(db, decision_id: int) -> tuple[Decimal | None, str]:
    """Realized T+1 executable P&L on the SAME convention V4.4B models:
    enter at ASK for a buy / BID for a sell, exit at BID for a long leg /
    ASK for a short leg. Returns (pnl, note); ``None`` whenever any leg
    lacks its required real side -- never a midpoint substitution."""
    entries = db.execute(
        text(
            """
            SELECT leg_index, action, quantity, bid, ask
            FROM entry_snapshot WHERE decision_id = :d ORDER BY leg_index
            """
        ),
        {"d": decision_id},
    ).mappings().all()
    exits = db.execute(
        text(
            """
            SELECT leg_index, quantity, multiplier, bid, ask
            FROM exit_snapshot WHERE decision_id = :d ORDER BY leg_index
            """
        ),
        {"d": decision_id},
    ).mappings().all()
    if not entries or not exits:
        return None, "no entry/exit evidence"
    exit_by_leg = {e["leg_index"]: e for e in exits}

    total = Decimal(0)
    for entry in entries:
        exit_row = exit_by_leg.get(entry["leg_index"])
        if exit_row is None:
            return None, f"leg {entry['leg_index']} has no exit evidence"
        action = str(entry["action"]).lower()
        entry_px = _decimal(entry["ask"]) if action == "buy" else _decimal(entry["bid"])
        exit_px = _decimal(exit_row["bid"]) if action == "buy" else _decimal(exit_row["ask"])
        if entry_px is None or exit_px is None:
            return None, f"leg {entry['leg_index']} missing a required executable side"
        qty = Decimal(str(entry["quantity"] or 1))
        mult = _decimal(exit_row["multiplier"]) or Decimal("100")
        sign = Decimal(1) if action == "buy" else Decimal(-1)
        total += sign * (exit_px - entry_px) * qty * mult
    return total, "executable convention, both sides real"


def run_diagnose(frozen: dict) -> None:
    db = SessionLocal()
    try:
        rows = [r for r in frozen["rows"] if r["status"] == "REPLAYABLE"
                and r.get("v4_4b_status") == "RANKABLE"]
        print()
        print("=" * 78)
        print("POST-HOC DIAGNOSTIC -- outcomes joined only AFTER the freeze")
        print("=" * 78)
        print(f"frozen ranking_version : {frozen['ranking_version']}")
        print(f"frozen at              : {frozen['frozen_at']}")
        print()
        header = (f"{'id':>5} {'ticker':<6} {'strategy':<20} {'v4.4b median':>13} "
                  f"{'realized P&L':>13} {'same sign?':>11}")
        print(header)
        agree = disagree = unknown = 0
        for r in rows:
            pnl, note = _realized_executable_pnl(db, r["decision_id"])
            modeled = r.get("median_executable_return")
            if pnl is None or modeled is None:
                unknown += 1
                print(f"{r['decision_id']:>5} {r['ticker']:<6} {str(r['v3_strategy']):<20} "
                      f"{(modeled or '-')[:13]:>13} {'-':>13} {'no data':>11}  ({note})")
                continue
            same = (Decimal(modeled) < 0) == (pnl < 0)
            agree += 1 if same else 0
            disagree += 0 if same else 1
            print(f"{r['decision_id']:>5} {r['ticker']:<6} {str(r['v3_strategy']):<20} "
                  f"{float(modeled):>13.4f} {float(pnl):>13.2f} {('YES' if same else 'NO'):>11}")
        print()
        print(f"sign agreement : {agree} agree / {disagree} disagree / {unknown} no outcome data")
        print()
        print("INTERPRETATION LIMITS (Section 44): this sample is far too small to support any")
        print("claim that V4 is profitable, beats V3, or has a higher win rate. V4.4B was also")
        print("developed in the same historical environment. This is a directional sanity check")
        print("only -- a structural correction requiring forward validation, nothing more.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("v4_4b_replay.json"))
    parser.add_argument("--in", dest="in_path", type=Path)
    args = parser.parse_args()

    if args.replay:
        frozen = run_replay(args.out)
        summarize(frozen)
        print(f"\nfrozen rankings written to {args.out}")
    elif args.diagnose:
        if not args.in_path or not args.in_path.exists():
            raise SystemExit("--diagnose requires --in pointing at a frozen pass-1 file")
        frozen = json.loads(args.in_path.read_text())
        summarize(frozen)
        run_diagnose(frozen)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
