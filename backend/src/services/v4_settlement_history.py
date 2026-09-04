"""One place that answers "which settlement row actually counts?".

A configuration's settlement history became append-only on 2026-09-04: a
failed attempt is immutable and is never rewritten, so a later end-of-day
recovery is appended as a NEW row that supersedes it. Exactly one attempt
per configuration may be SETTLED (a partial unique index enforces that), but
read models still see both rows and must not count a configuration twice --
once as failed and again as settled.

The settlement of record is simply the latest attempt: settlement rows are
only ever appended, so the highest id for a configuration is the one that
describes where that position actually ended up.
"""

from collections.abc import Iterable

from models.v4_shadow import V4ShadowConfigSettlement


def effective_settlements(
    rows: Iterable[V4ShadowConfigSettlement],
) -> list[V4ShadowConfigSettlement]:
    """The settlement of record per configuration, newest attempt winning.
    Superseded attempts are dropped from the result, never from the
    database."""
    latest: dict[int, V4ShadowConfigSettlement] = {}
    for row in rows:
        current = latest.get(row.shadow_config_result_id)
        if current is None or (row.id or 0) > (current.id or 0):
            latest[row.shadow_config_result_id] = row
    return sorted(latest.values(), key=lambda r: r.id or 0)
