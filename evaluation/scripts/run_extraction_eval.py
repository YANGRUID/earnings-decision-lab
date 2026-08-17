"""Structured-extraction evaluation: for each labeled (filing, chunk range)
item, runs the real services.extraction.extract_guidance against the
configured LLM and checks:

  - non_capex_fields_correctly_null: revenue/eps/gross_margin are all None.
    Confirmed via repeated corpus-wide phrase search (see
    evaluation/datasets/extraction_ground_truth.jsonl notes) that none of
    the ingested filing text states an explicit forward revenue, EPS, or
    gross-margin figure -- a non-null value here means either genuinely new
    guidance text or the model inventing a number, either way worth a
    human look.
  - capex_correct: capex matches expected_capex_low/high (or is null when
    both are null). Three of the eight items *do* have real, explicit
    capex guidance -- Micron's MD&A states a specific forward capex figure
    in "Liquidity and Capital Resources" every quarter, which the first
    version of this dataset missed (an incomplete corpus search, not a
    system bug -- see the ext-mu-* notes). This is why capex gets its own
    accuracy figure instead of being folded into a blanket "all null" check
    like the other three fields.
  - tone_plausible / key_driver_keyword_hit: as before.

Persists one AIExtraction row per item, same as the real extraction
service always does -- this is not a dry run. Makes one real, billed LLM
call per item.
"""

from decimal import Decimal

from _bootstrap import get_db_session, get_llm_and_embedder, load_jsonl

from evaluation.models import ExtractionItemResult, ExtractionSummary
from models.document_chunk import DocumentChunk
from services.extraction import extract_guidance

CAPEX_TOLERANCE = Decimal("0.1")


def _as_decimal(value) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _range_is_empty(value: dict | None) -> bool:
    if value is None:
        return True
    return value.get("low") is None and value.get("high") is None


def _capex_matches(extracted: dict | None, expected_low: float | None, expected_high: float | None) -> bool:
    if expected_low is None and expected_high is None:
        return _range_is_empty(extracted)
    if extracted is None:
        return False
    got_low, got_high = _as_decimal(extracted.get("low")), _as_decimal(extracted.get("high"))
    want_low, want_high = _as_decimal(expected_low), _as_decimal(expected_high)
    low_ok = (got_low is None and want_low is None) or (
        got_low is not None and want_low is not None and abs(got_low - want_low) <= CAPEX_TOLERANCE
    )
    high_ok = (got_high is None and want_high is None) or (
        got_high is not None
        and want_high is not None
        and abs(got_high - want_high) <= CAPEX_TOLERANCE
    )
    return low_ok and high_ok


def run() -> ExtractionSummary:
    db = get_db_session()
    llm, _embedder = get_llm_and_embedder()
    items = load_jsonl("extraction_ground_truth.jsonl")

    results: list[ExtractionItemResult] = []
    for item in items:
        chunks = (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.filing_id == item["filing_id"],
                DocumentChunk.id >= item["chunk_id_start"],
                DocumentChunk.id <= item["chunk_id_end"],
            )
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        extraction = extract_guidance(db, llm, item["filing_id"], item["company_id"], chunks)
        data = extraction.extracted_data

        non_capex_fields_correctly_null = all(
            _range_is_empty(data.get(field)) for field in ("revenue", "eps", "gross_margin")
        )
        capex = data.get("capex")
        capex_correct = _capex_matches(
            capex, item["expected_capex_low"], item["expected_capex_high"]
        )

        tone = (data.get("management_tone") or {}).get("overall")
        tone_plausible = tone in item["acceptable_tones"]

        haystack = " ".join(data.get("key_drivers", []) + data.get("important_topics", [])).lower()
        keyword_hit = any(kw.lower() in haystack for kw in item["expected_keyword_hits"])

        results.append(
            ExtractionItemResult(
                id=item["id"],
                ticker=item["ticker"],
                non_capex_fields_correctly_null=non_capex_fields_correctly_null,
                capex_correct=capex_correct,
                extracted_capex_low=float(capex["low"]) if capex and capex.get("low") else None,
                extracted_capex_high=float(capex["high"]) if capex and capex.get("high") else None,
                tone=tone,
                tone_plausible=tone_plausible,
                key_driver_keyword_hit=keyword_hit,
            )
        )

    db.commit()
    db.close()
    n = len(results)
    return ExtractionSummary(
        item_count=n,
        non_capex_null_accuracy=sum(1 for r in results if r.non_capex_fields_correctly_null) / n,
        capex_accuracy=sum(1 for r in results if r.capex_correct) / n,
        tone_plausibility_rate=sum(1 for r in results if r.tone_plausible) / n,
        keyword_hit_rate=sum(1 for r in results if r.key_driver_keyword_hit) / n,
        items=results,
    )


if __name__ == "__main__":
    summary = run()
    print(summary.model_dump_json(indent=2))
