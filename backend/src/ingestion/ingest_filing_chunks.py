"""Phase 5 ingestion: fetch real filing HTML for every Filing row that
doesn't have chunks yet, parse -> chunk -> embed -> persist.

Run: uv run python -m ingestion.ingest_filing_chunks

Pipeline: discovery is Filing rows already seeded by bootstrap_phase1.py
(real SEC EDGAR metadata) -> download real HTML -> parse (rag.parsing) ->
chunk (rag.chunking) -> embed (rag.embeddings, local model, no API key) ->
persist (DocumentChunk). Every step operates on real filings; nothing here
is fixture or synthetic data.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from core.config import get_settings
from db.session import SessionLocal
from models.document_chunk import DocumentChunk
from models.filing import Filing
from providers.sec_edgar import SECEdgarProvider
from rag.chunking import chunk_sections
from rag.embeddings import FastEmbedProvider
from rag.parsing import html_to_text, split_into_sections

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_filing_chunks")


def _filings_without_chunks(db: Session) -> list[Filing]:
    chunked_filing_ids = {row[0] for row in db.query(DocumentChunk.filing_id).distinct().all()}
    all_filings = db.query(Filing).all()
    return [f for f in all_filings if f.id not in chunked_filing_ids]


def ingest_filing(
    db: Session, edgar: SECEdgarProvider, embedder: FastEmbedProvider, filing: Filing
) -> int:
    html = edgar.get_filing_html(filing.source_url)
    text = html_to_text(html)
    sections = split_into_sections(text)
    chunks = chunk_sections(sections)

    if not chunks:
        log.warning("  no chunks produced for filing %s (empty/unparseable text)", filing.id)
        return 0

    embeddings = embedder.embed([c.text for c in chunks])
    retrieved_at = datetime.now(UTC)

    for chunk, embedding in zip(chunks, embeddings, strict=True):
        db.add(
            DocumentChunk(
                filing_id=filing.id,
                company_id=filing.company_id,
                chunk_index=chunk.chunk_index,
                section=chunk.section,
                text=chunk.text,
                token_count=chunk.token_count,
                embedding=embedding,
                embedding_model=embedder.model_name,
                retrieved_at=retrieved_at,
            )
        )
    db.commit()
    return len(chunks)


def main() -> None:
    settings = get_settings()
    edgar = SECEdgarProvider(user_agent=settings.sec_edgar_user_agent)
    log.info(
        "loading local embedding model (%s) — first run downloads weights",
        FastEmbedProvider.model_name,
    )
    embedder = FastEmbedProvider()
    db = SessionLocal()

    try:
        targets = _filings_without_chunks(db)
        log.info("=== %d filings need chunking ===", len(targets))
        total_chunks = 0
        for filing in targets:
            log.info(
                "company %s filing %s (%s, %s)",
                filing.company_id,
                filing.id,
                filing.filing_type,
                filing.filing_date,
            )
            count = ingest_filing(db, edgar, embedder, filing)
            log.info("  -> %d chunks", count)
            total_chunks += count
        log.info("ingestion complete: %d chunks across %d filings", total_chunks, len(targets))
    finally:
        db.close()


if __name__ == "__main__":
    main()
